using System.Runtime.CompilerServices;

[assembly: CollectionBehavior(DisableTestParallelization = true)] // Settings.FilePath is shared

namespace Hover.Tests;

public sealed class SessionTests
{
    const string DocsUrl = "https://docs.python.org/3/library/os.path.html";
    static readonly string[] AnswerChunks = ["os.path.join: ", "Python function"];
    static readonly Settings Defaults = new();
    static readonly Settings Compress = new() { Mode = "compress" };

    readonly List<(string Model, List<Message> Messages)> chats = [];
    readonly List<string> queries = [];
    IReadOnlyList<SearchResult> searchResults = [new("os.path", DocsUrl, "os.path.join(path, *paths) joins path segments.")];

    Session NewSession() => new(FakeChat, FakeSearch);

    async IAsyncEnumerable<string> FakeChat(Settings settings, string model, IReadOnlyList<Message> messages,
                                            [EnumeratorCancellation] CancellationToken cancel)
    {
        chats.Add((model, messages.ToList()));
        foreach (var chunk in AnswerChunks)
        {
            await Task.Yield();
            cancel.ThrowIfCancellationRequested();
            yield return chunk;
        }
    }

    Task<IReadOnlyList<SearchResult>> FakeSearch(string query, int count, CancellationToken cancel)
    {
        queries.Add(query);
        return Task.FromResult(searchResults);
    }

    static Task<Answer> Ask(Session session, Item item, Settings settings, string? mode = null,
                            Action<string>? onToken = null, CancellationToken cancel = default)
    {
        return session.AskAsync(item, settings, mode, onToken ?? (_ => { }), cancel);
    }

    [Fact]
    public async Task Define_IsGroundedStreamedAndRemembered_WithoutSearchSnippets()
    {
        var session = NewSession();
        var tokens = new List<string>();

        var answer = await Ask(session, new TextItem("os.path.join"), Defaults, onToken: tokens.Add);

        Assert.Equal(("os.path.join: Python function", DocsUrl, "define"), (answer.Text, answer.Link, answer.Mode));
        Assert.Equal(AnswerChunks, tokens);
        Assert.Equal(Defaults.Model, chats[0].Model);
        Assert.Equal(Prompts.Define, chats[0].Messages[0].Content);
        Assert.Contains("joins path segments", chats[0].Messages[^1].Content);
        Assert.Equal(["Copied: os.path.join", answer.Text], session.History.Select(m => m.Content));
    }

    [Fact]
    public async Task Offline_NeverSearches()
    {
        var answer = await Ask(NewSession(), new TextItem("sonder"), Defaults with { Offline = true });
        Assert.Empty(queries);
        Assert.Equal("", answer.Link);
        Assert.Contains("offline", chats[0].Messages[^1].Content);
    }

    [Fact]
    public async Task UnsafeLinks_AreNeverOffered()
    {
        searchResults = [new("x", "javascript:alert(1)", "x"), new("x", "https://a.example/x\"\tfile:///C:/win.ini", "x")];
        Assert.Equal("", (await Ask(NewSession(), new TextItem("x"), Defaults)).Link);
    }

    [Fact]
    public async Task Compress_SeesTheConversationAsOneTranscript()
    {
        var session = NewSession();
        await Ask(session, new TextItem("mutex"), Defaults);
        await Ask(session, new TextItem("deadlock"), Compress);

        var messages = chats[1].Messages;
        Assert.Equal(["system", "user"], messages.Select(m => m.Role));
        Assert.StartsWith("Conversation so far:\nCopied: mutex\n\nos.path.join: Python function", messages[1].Content);
        Assert.EndsWith(Session.WholeConversation, messages[1].Content);
    }

    [Fact]
    public async Task AskingTheSameItemAgain_ReplacesItsTurn()
    {
        var session = NewSession();
        await Ask(session, new TextItem("mutex"), Defaults);
        await Ask(session, new TextItem("mutex"), Compress); // e.g. Shift+Tab to the next mode
        Assert.Equal(1, session.Turns);
        Assert.DoesNotContain("Conversation so far", chats[1].Messages[^1].Content); // its old turn isn't context for itself

        byte[] first = [1], second = [2];
        await Ask(session, new TextItem("deadlock"), Defaults);
        await Ask(session, new Shot(first), Defaults);
        await Ask(session, new Shot(second), Defaults); // different screenshots are different items, even without OCR text
        Assert.Equal(4, session.Turns);
    }

    [Fact]
    public async Task ClearingDuringAStream_DiscardsThatAnswer()
    {
        Session? session = null;
        async IAsyncEnumerable<string> ClearThenAnswer(Settings s, string m, IReadOnlyList<Message> messages,
                                                       [EnumeratorCancellation] CancellationToken c)
        {
            session!.Clear(); // the user cleared the context while this answer was streaming
            await Task.Yield();
            yield return "late answer";
        }

        session = new Session(ClearThenAnswer, FakeSearch);
        Assert.Equal("late answer", (await Ask(session, new TextItem("x"), Defaults with { Offline = true })).Text);
        Assert.Empty(session.History);
    }

    [Fact]
    public async Task Cancelling_StopsStreamingAndKeepsNothing()
    {
        var session = NewSession();
        using var cancel = new CancellationTokenSource();

        var answer = await Ask(session, new TextItem("os.path.join"), Defaults, onToken: _ => cancel.Cancel(),
                               cancel: cancel.Token);

        Assert.True(answer.Cancelled);
        Assert.Equal("os.path.join:", answer.Text);
        Assert.Empty(session.History);
    }

    [Fact]
    public async Task AServerTimeout_IsAnError_NotASilentCancel()
    {
        async IAsyncEnumerable<string> TimesOut(Settings s, string m, IReadOnlyList<Message> messages,
                                                [EnumeratorCancellation] CancellationToken c)
        {
            await Task.Yield();
            throw new TaskCanceledException("The request was canceled due to the configured HttpClient.Timeout");
#pragma warning disable CS0162 // an iterator needs a yield
            yield break;
#pragma warning restore CS0162
        }

        var session = new Session(TimesOut, FakeSearch);
        await Assert.ThrowsAsync<TaskCanceledException>(() => Ask(session, new TextItem("x"), Defaults with { Offline = true }));
    }

    [Fact]
    public async Task ACancelledLookup_NeverSearches()
    {
        using var cancel = new CancellationTokenSource();
        cancel.Cancel();
        Assert.True((await Ask(NewSession(), new TextItem("mutex"), Defaults, cancel: cancel.Token)).Cancelled);
        Assert.Empty(queries);
        Assert.Empty(chats);
    }

    [Fact]
    public async Task Screenshots_GoToTheVisionModel_WithTheirOcrText()
    {
        var session = NewSession();
        await Ask(session, new Shot([1], "TypeError: 'NoneType' object is not subscriptable"), Defaults);
        await Ask(session, new Shot([2], "ok"), Defaults);

        Assert.Equal(Defaults.VisionModel, chats[0].Model);
        Assert.Equal([1], chats[0].Messages[^1].Image);
        Assert.Contains("OCR transcript", chats[0].Messages[^1].Content);
        Assert.DoesNotContain("OCR transcript", chats[1].Messages[^1].Content); // too little text to be worth it
        Assert.StartsWith("[screenshot]", session.History[0].Content);
    }

    [Fact]
    public async Task PerModeModel_AndLanguagePlaceholder()
    {
        var modes = Prompts.DefaultModes();
        modes["translate"] = modes["translate"] with { Model = "custom:1b" };

        await Ask(NewSession(), new TextItem("bonjour"), Defaults with { Modes = modes, Language = "German" }, "translate");

        Assert.Equal("custom:1b", chats[0].Model);
        Assert.Contains("German", chats[0].Messages[0].Content);
        Assert.DoesNotContain("{language}", chats[0].Messages[0].Content);
    }

    [Fact]
    public async Task History_IsTrimmedToWholePairs()
    {
        var session = NewSession();
        for (var n = 0; n < 8; n++)
            await Ask(session, new TextItem($"term {n}"), Defaults with { ContextMessages = 4 });
        Assert.Equal(2, session.Turns);

        var messages = Enumerable.Range(0, 6).Select(n => new Message("user", $"{n}")).ToList();
        Assert.Equal(["4", "5"], Session.Trimmed(messages, 3).Select(m => m.Content));
        Assert.Equal(6, Session.Trimmed(messages, 12).Count);
        Assert.Empty(Session.Trimmed(messages, -4));
    }

    [Fact]
    public void InlineModePrefix_PicksAModeForOneItem()
    {
        var modes = Prompts.DefaultModes();
        Assert.Equal(("translate", "bonjour"), Session.SplitInlineMode(">>translate: bonjour", modes));
        Assert.Equal((null, ">>nope: bonjour"), Session.SplitInlineMode(">>nope: bonjour", modes));
        Assert.Equal((null, "plain"), Session.SplitInlineMode("plain", modes));
    }
}
