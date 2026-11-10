using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace Hover;

/// <summary>One mode: a system prompt plus optional per-mode overrides.</summary>
public record ModeConfig
{
    public string Prompt { get; init; } = "";
    public bool Search { get; init; }
    public string? Hotkey { get; init; }
    public string? Model { get; init; }
    public string? VisionModel { get; init; }
}

/// <summary>
/// User settings, stored in %APPDATA%\Hover\settings.json (same keys as the Python version, so old files keep working).
/// Every property has a default; a key missing from the file simply keeps its default.
/// </summary>
public record Settings
{
    public static readonly string DataDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "Hover");
    public static string FilePath { get; set; } = Path.Combine(DataDir, "settings.json"); // tests point this elsewhere

    public string Mode { get; init; } = "define";
    public Dictionary<string, ModeConfig> Modes { get; init; } = Prompts.DefaultModes();
    public Dictionary<string, string> Hotkeys { get; init; } = DefaultHotkeys();
    public string Provider { get; init; } = "ollama";
    public string OllamaHost { get; init; } = "http://127.0.0.1:11434";
    public string BaseUrl { get; init; } = "";
    public string ApiKeyEnv { get; init; } = "OPENAI_API_KEY";
    public string Model { get; init; } = "qwen3:4b-instruct-2507-q4_K_M";
    public string VisionModel { get; init; } = "qwen3-vl:4b-instruct";
    public double Temperature { get; init; } = 0.3;
    public int MaxTokens { get; init; } = 200;
    public int SearchResults { get; init; } = 3;
    public int MaxChars { get; init; } = 2000;
    public int ContextMessages { get; init; } = 12;
    public int HideAfterSeconds { get; init; } = 30;
    public string Language { get; init; } = "Spanish";
    public bool Offline { get; init; }
    public bool CopyFallback { get; init; } = true;
    public bool CtrlCFallback { get; init; }
    public bool Glossary { get; init; } = true;

    static readonly JsonSerializerOptions Json = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower, // "max_tokens", "ollama_host", ...
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        WriteIndented = true,
    };

    static Dictionary<string, string> DefaultHotkeys() => new()
    {
        ["lookup"] = "<ctrl>+<alt>+h",
        ["hover"] = "<ctrl>+<alt>+j",
        ["snip"] = "<ctrl>+<alt>+s",
    };

    /// <summary>The settings file merged over the defaults (the file is created on first run).</summary>
    public static Settings Load()
    {
        if (!File.Exists(FilePath))
            Save(new Settings());

        var loaded = JsonSerializer.Deserialize<Settings>(File.ReadAllText(FilePath), Json)
                     ?? throw new JsonException("the file is empty");
        if (loaded.Modes is null || loaded.Modes.Count == 0)
            throw new JsonException("\"modes\" must map names to {\"prompt\": ...} objects");
        if (!loaded.Modes.ContainsKey(loaded.Mode))
            throw new JsonException($"\"mode\" is \"{loaded.Mode}\", which isn't one of the modes");

        // A file that sets only some hotkeys keeps the defaults for the others.
        var hotkeys = DefaultHotkeys();
        foreach (var (name, combo) in loaded.Hotkeys ?? [])
            hotkeys[name] = combo;
        return loaded with { Hotkeys = hotkeys };
    }

    /// <summary>
    /// (settings, error). A broken file falls back to the defaults with web search OFF: the user may have chosen
    /// strict offline in the file we can't read, so we fail closed.
    /// </summary>
    public static (Settings Settings, string Error) SafeLoad()
    {
        try
        {
            return (Load(), "");
        }
        catch (Exception error) when (error is IOException or JsonException or UnauthorizedAccessException)
        {
            return (new Settings { Offline = true }, $"settings.json error: {error.Message}");
        }
    }

    public static void Save(Settings settings) => WriteAtomically(JsonSerializer.Serialize(settings, Json));

    /// <summary>Change one top-level key, keeping everything else the user wrote (including unknown keys).</summary>
    public static void Update(string key, JsonNode? value)
    {
        JsonObject file;
        if (File.Exists(FilePath))
            file = JsonNode.Parse(File.ReadAllText(FilePath)) as JsonObject
                   ?? throw new JsonException("settings.json must contain a JSON object");
        else
            file = JsonSerializer.SerializeToNode(new Settings(), Json)!.AsObject();
        file[key] = value;
        WriteAtomically(file.ToJsonString(Json));
    }

    /// <summary>Write via a temporary file, so a crash or a simultaneous edit can't leave a truncated file.</summary>
    static void WriteAtomically(string text)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
        var temporary = FilePath + ".tmp";
        File.WriteAllText(temporary, text);
        File.Move(temporary, FilePath, overwrite: true);
    }
}

public static class Prompts
{
    public const string Define =
        "You are a terse dictionary and documentation lookup. The user copied a term: an English word or phrase, " +
        "or a programming identifier, command, or type. Reply in plain text, no preamble, in at most 3 short lines. " +
        "Line 1 says what it is: part of speech, or language or library and kind (function, class, type, keyword, " +
        "command). Then give a succinct definition; for code include the signature or usage. Base the answer on the " +
        "search results when they are relevant and never invent APIs; if they don't support an answer, start with " +
        "'Not sure:'. Items arrive one at a time and may also be a screenshot or a question about earlier items: " +
        "describe screenshots the same way, and answer questions using the conversation so far.";

    public const string Explain =
        "You explain code, error messages, and technical text the user copied or captured. In at most 4 short lines: " +
        "say what it is, what it does or means, and the most likely fix or next step for errors. Use `backticks` for " +
        "identifiers. Base the answer on the search results when relevant; if unsure, start with 'Not sure:'.";

    public const string Compress =
        "You compress context. Ignore the format of earlier replies. Merge everything the user has copied in this " +
        "conversation, including the latest item and any screenshots, into one dense paragraph of at most 3 sentences " +
        "that keeps every key fact, name, number and code identifier. Plain text, no labels, no preamble.";

    public const string Translate =
        "Translate the latest item into {language}. If it is already in {language}, translate it into English. Reply " +
        "with the translation only, then one short line with any nuance worth knowing.";

    public static Dictionary<string, ModeConfig> DefaultModes() => new()
    {
        ["define"] = new ModeConfig { Prompt = Define, Search = true },
        ["explain"] = new ModeConfig { Prompt = Explain, Search = true },
        ["compress"] = new ModeConfig { Prompt = Compress, Search = false },
        ["translate"] = new ModeConfig { Prompt = Translate, Search = false },
    };
}
