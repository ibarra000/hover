using System.IO;
using System.Net.Http;
using System.Net.Http.Json;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Hover;

/// <summary>Something to look up: copied or selected text, or a screenshot.</summary>
public abstract record Item;
public sealed record TextItem(string Text) : Item;
public sealed record Shot(byte[] Png, string OcrText = "") : Item;

public sealed record Answer(string Text, string Link, string Mode, bool Cancelled = false);
public sealed record Message(string Role, string Content, byte[]? Image = null);

/// <summary>Streams answer text chunks for a conversation (swapped for a fake in tests).</summary>
public delegate IAsyncEnumerable<string> ChatStream(Settings settings, string model, IReadOnlyList<Message> messages,
                                                    CancellationToken cancel);

/// <summary>Talks to Ollama, or to any OpenAI-compatible server (LM Studio, OpenRouter, OpenAI, ...).</summary>
public static class Models
{
    const int ContextWindow = 8192; // fixed: changing it would force Ollama to reload the models
    static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(120) }; // a cold model load takes ~15 s

    public static IAsyncEnumerable<string> Stream(Settings settings, string model, IReadOnlyList<Message> messages,
                                                  CancellationToken cancel)
    {
        return settings.Provider == "openai"
            ? OpenAiStream(settings, model, messages, cancel)
            : OllamaStream(settings, model, messages, cancel);
    }

    static async IAsyncEnumerable<string> OllamaStream(Settings settings, string model, IReadOnlyList<Message> messages,
                                                       [EnumeratorCancellation] CancellationToken cancel)
    {
        var body = new
        {
            model,
            stream = true,
            think = false,   // Ollama turns thinking on by default for models that support it
            keep_alive = -1, // stay loaded forever
            options = new { temperature = settings.Temperature, num_predict = settings.MaxTokens, num_ctx = ContextWindow },
            messages = messages.Select(OllamaMessage),
        };
        await foreach (var line in PostAndReadLines($"{settings.OllamaHost}/api/chat", body, null, cancel))
        {
            using var json = JsonDocument.Parse(line);
            if (json.RootElement.TryGetProperty("error", out var error))
                throw new InvalidOperationException(error.GetString());
            yield return json.RootElement.GetProperty("message").GetProperty("content").GetString() ?? "";
        }
    }

    static object OllamaMessage(Message message)
    {
        if (message.Image is null)
            return new { role = message.Role, content = message.Content };
        return new { role = message.Role, content = message.Content, images = new[] { Convert.ToBase64String(message.Image) } };
    }

    static async IAsyncEnumerable<string> OpenAiStream(Settings settings, string model, IReadOnlyList<Message> messages,
                                                       [EnumeratorCancellation] CancellationToken cancel)
    {
        var body = new
        {
            model,
            stream = true,
            temperature = settings.Temperature,
            max_tokens = settings.MaxTokens,
            messages = messages.Select(OpenAiMessage),
        };
        var key = Environment.GetEnvironmentVariable(settings.ApiKeyEnv) ?? "none"; // keys never live in settings.json
        var url = $"{settings.BaseUrl.TrimEnd('/')}/chat/completions";
        await foreach (var line in PostAndReadLines(url, body, key, cancel))
        {
            // Server-sent events: "data: {...}" lines, ending with "data: [DONE]".
            if (!line.StartsWith("data: ") || line == "data: [DONE]")
                continue;
            using var json = JsonDocument.Parse(line["data: ".Length..]);
            var choices = json.RootElement.GetProperty("choices");
            if (choices.GetArrayLength() > 0 && choices[0].GetProperty("delta").TryGetProperty("content", out var text))
                yield return text.GetString() ?? "";
        }
    }

    /// <summary>Ollama-style message (optional image bytes) in OpenAI's format, where images are data URLs.</summary>
    public static object OpenAiMessage(Message message)
    {
        if (message.Image is null)
            return new { role = message.Role, content = message.Content };
        var imageUrl = $"data:image/png;base64,{Convert.ToBase64String(message.Image)}";
        object[] parts =
        [
            new { type = "text", text = message.Content },
            new { type = "image_url", image_url = new { url = imageUrl } },
        ];
        return new { role = message.Role, content = parts };
    }

    static async IAsyncEnumerable<string> PostAndReadLines(string url, object body, string? bearer,
                                                           [EnumeratorCancellation] CancellationToken cancel)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, url) { Content = JsonContent.Create(body) };
        if (bearer is not null)
            request.Headers.Authorization = new("Bearer", bearer);
        using var response = await Http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancel);
        if (!response.IsSuccessStatusCode)
            throw new HttpRequestException($"{(int)response.StatusCode}: {await response.Content.ReadAsStringAsync(cancel)}");

        using var reader = new StreamReader(await response.Content.ReadAsStreamAsync(cancel));
        while (await reader.ReadLineAsync(cancel) is { } line)
        {
            if (line.Length > 0)
                yield return line;
        }
    }

    /// <summary>Load both Ollama models (and the vision encoder) and pin them in memory. Returns an error or "".</summary>
    public static async Task<string> WarmUp(Settings settings, byte[] blankPng)
    {
        if (settings.Provider != "ollama")
            return "";
        try
        {
            var options = new { num_ctx = ContextWindow, num_predict = 1 };
            await PostOnce($"{settings.OllamaHost}/api/generate", new { model = settings.Model, keep_alive = -1, options });
            await PostOnce($"{settings.OllamaHost}/api/generate", new
            {
                model = settings.VisionModel, keep_alive = -1, options, stream = false, prompt = ".",
                images = new[] { Convert.ToBase64String(blankPng) }, // a real image also warms the vision encoder
            });
            return "";
        }
        catch (Exception error) when (error is HttpRequestException or TaskCanceledException)
        {
            return $"Model not ready: {error.Message}";
        }
    }

    static async Task PostOnce(string url, object body)
    {
        using var response = await Http.PostAsJsonAsync(url, body);
        if (!response.IsSuccessStatusCode)
            throw new HttpRequestException($"{(int)response.StatusCode}: {await response.Content.ReadAsStringAsync()}");
    }
}

/// <summary>
/// One running conversation: each lookup is a follow-up to the earlier ones until the context is cleared.
/// The lock is only held to read or save the history, never while searching or streaming.
/// </summary>
public sealed class Session(ChatStream chat, WebSearch search)
{
    const int MinOcrChars = 20; // below this, OCR found no real text and the image speaks for itself
    public const string WholeConversation =
        "Apply your instructions to everything copied in this conversation so far, not just this item.";
    static readonly Regex InlineMode = new(@"^>>\s*(\w+)\s*:\s*", RegexOptions.Singleline);

    readonly Lock gate = new();
    List<Message> history = []; // text only: no search snippets or image bytes are kept
    Item? lastItem;             // what the newest turn is about
    int generation;             // bumped by Clear(), so an answer that was streaming during a clear isn't saved

    public int Turns
    {
        get { lock (gate) return history.Count / 2; }
    }

    public IReadOnlyList<Message> History
    {
        get { lock (gate) return history.ToList(); }
    }

    public void Clear()
    {
        lock (gate)
        {
            history = [];
            lastItem = null;
            generation++;
        }
    }

    /// <summary>"&gt;&gt;translate: bonjour" picks a mode for one item: returns (mode, text), or (null, text).</summary>
    public static (string? Mode, string Text) SplitInlineMode(string text, IReadOnlyDictionary<string, ModeConfig> modes)
    {
        var match = InlineMode.Match(text);
        if (match.Success && modes.ContainsKey(match.Groups[1].Value))
            return (match.Groups[1].Value, text[match.Length..]);
        return (null, text);
    }

    /// <summary>The newest `keep` messages, in whole user/assistant pairs.</summary>
    public static List<Message> Trimmed(List<Message> messages, int keep)
    {
        keep = Math.Max(0, keep);
        keep -= keep % 2;
        return messages.Skip(Math.Max(0, messages.Count - keep)).ToList();
    }

    /// <summary>
    /// Answer `item` with a mode's prompt, streaming chunks to onToken. Cancelling stops early and keeps nothing.
    /// Asking about the same item again (a repeat press, or Shift+Tab to another mode) replaces its earlier turn.
    /// </summary>
    public async Task<Answer> AskAsync(Item item, Settings settings, string? mode, Action<string> onToken,
                                       CancellationToken cancel)
    {
        mode ??= settings.Mode;
        var config = settings.Modes[mode];
        var stopped = new Answer("", "", mode, Cancelled: true);
        if (cancel.IsCancellationRequested)
            return stopped; // superseded before it started: skip the search and the model call

        var (model, note, current, results) = await Prompt(item, settings, mode, config, cancel);
        List<Message> earlier;
        int startedAt;
        lock (gate)
        {
            earlier = WithoutTurnAbout(item);
            startedAt = generation;
        }

        var system = new Message("system", config.Prompt.Replace("{language}", settings.Language));
        var chunks = new List<string>();
        try
        {
            await foreach (var chunk in chat(settings, model, BuildMessages(system, earlier, current, mode), cancel))
            {
                chunks.Add(chunk);
                onToken(chunk);
            }
        }
        catch (OperationCanceledException) when (cancel.IsCancellationRequested)
        {
            // Stopped by the user or a newer lookup. (A server timeout is also an OperationCanceledException,
            // but without our token set: that one propagates and shows as an error.)
            return stopped with { Text = string.Concat(chunks).Trim() };
        }

        var answer = new Answer(string.Concat(chunks).Trim(), SafeLinks.First(results), mode);
        lock (gate)
        {
            if (startedAt == generation) // the context wasn't cleared while we were streaming
            {
                var turn = new[] { new Message("user", note), new Message("assistant", answer.Text) };
                history = Trimmed([.. WithoutTurnAbout(item), .. turn], settings.ContextMessages);
                lastItem = item;
            }
        }
        return answer;
    }

    /// <summary>The history minus its newest turn if that turn was about this same item. Call with the lock held.</summary>
    List<Message> WithoutTurnAbout(Item item)
    {
        if (Equals(item, lastItem) && history.Count >= 2)
            return history[..^2];
        return history.ToList();
    }

    static List<Message> BuildMessages(Message system, List<Message> earlier, Message current, string mode)
    {
        if (mode == "define" || earlier.Count == 0)
            return [system, .. earlier, current];

        // Other modes get the conversation as one folded message, so they don't copy earlier reply formats
        // (and strict chat templates stay happy).
        var transcript = string.Join("\n\n", earlier.Select(m => m.Content));
        return [system, current with { Content = $"Conversation so far:\n{transcript}\n\n{current.Content}" }];
    }

    /// <summary>The model to use, the note kept in the history, the message to send, and any search results.</summary>
    async Task<(string Model, string Note, Message Current, IReadOnlyList<SearchResult> Results)> Prompt(
        Item item, Settings settings, string mode, ModeConfig config, CancellationToken cancel)
    {
        if (item is Shot shot)
        {
            var ocr = shot.OcrText.Length >= MinOcrChars ? $"\n\nOCR transcript (may lack indentation):\n{shot.OcrText}" : "";
            var visionModel = config.VisionModel ?? settings.VisionModel;
            return (visionModel, $"[screenshot]{ocr}", new Message("user", $"Screenshot{ocr}", shot.Png), []);
        }

        var text = ((TextItem)item).Text;
        var note = $"Copied: {text}";
        var model = config.Model ?? settings.Model;
        var searching = config.Search && !settings.Offline;
        if (!searching)
        {
            var task = mode == "define" ? "(offline: answer from your own knowledge)" : WholeConversation;
            return (model, note, new Message("user", $"{note}\n\n{task}"), []);
        }

        var results = await search(text, settings.SearchResults, cancel);
        var snippets = results.Count > 0
            ? string.Join("\n", results.Select(r => $"- {r.Title}: {r.Snippet}"))
            : "(no search results)";
        return (model, note, new Message("user", $"{note}\n\nSearch results:\n{snippets}"), results);
    }
}
