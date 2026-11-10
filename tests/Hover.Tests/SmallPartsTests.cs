using System.IO;
using System.Text.Json;
using static Hover.Native;

namespace Hover.Tests;

public sealed class SettingsTests : IDisposable
{
    readonly string folder = Directory.CreateTempSubdirectory("hover-tests").FullName;

    public SettingsTests() => Settings.FilePath = Path.Combine(folder, "settings.json");

    public void Dispose() => Directory.Delete(folder, recursive: true);

    [Fact]
    public void FirstLoad_WritesTheDefaults_InTheOldSnakeCaseFormat()
    {
        var loaded = Settings.Load();
        Assert.Equal("define", loaded.Mode);
        var file = File.ReadAllText(Settings.FilePath);
        Assert.Contains("\"max_tokens\": 200", file);
        Assert.Contains("\"ollama_host\"", file);
    }

    [Fact]
    public void UserEdits_OverrideDefaults_AndMissingKeysFallBack()
    {
        File.WriteAllText(Settings.FilePath, """{"temperature": 0.9, "hotkeys": {"snip": "<ctrl>+<alt>+x"}, "extra": 1}""");

        var loaded = Settings.Load();

        Assert.Equal(0.9, loaded.Temperature);
        Assert.Equal(new Settings().Model, loaded.Model);
        Assert.Equal("<ctrl>+<alt>+x", loaded.Hotkeys["snip"]);
        Assert.Equal("<ctrl>+<alt>+h", loaded.Hotkeys["lookup"]); // unset hotkeys keep their defaults
    }

    [Fact]
    public void Update_ChangesOneKey_AndKeepsEverythingElse()
    {
        File.WriteAllText(Settings.FilePath, """{"language": "German", "my_note": "keep me"}""");
        Settings.Update("mode", "translate");

        var file = File.ReadAllText(Settings.FilePath);
        Assert.Contains("keep me", file);
        Assert.Equal(("translate", "German"), (Settings.Load().Mode, Settings.Load().Language));
    }

    [Fact]
    public void ABrokenFile_FallsBackToDefaults_WithWebSearchOff()
    {
        File.WriteAllText(Settings.FilePath, "{ not json");
        var (settings, error) = Settings.SafeLoad();
        Assert.True(settings.Offline); // fail closed: the user may have chosen strict offline in the unreadable file
        Assert.StartsWith("settings.json error:", error);

        File.WriteAllText(Settings.FilePath, """{"modes": null}""");
        Assert.StartsWith("settings.json error:", Settings.SafeLoad().Error);

        File.WriteAllText(Settings.FilePath, """{"mode": "deleted-mode"}""");
        Assert.Contains("isn't one of the modes", Settings.SafeLoad().Error);
    }
}

public sealed class SearchTests
{
    [Fact]
    public void Parse_ReadsTitlesRealUrlsAndSnippets_FromDuckDuckGoHtml()
    {
        var html = File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "Fixtures", "ddg.html"));

        var results = DuckDuckGo.Parse(html).ToList();

        Assert.True(results.Count >= 3);
        Assert.Contains(results, r => r.Url == "https://docs.python.org/3/library/os.path.html");
        Assert.All(results, r => Assert.DoesNotContain("<b>", r.Snippet));
        Assert.All(results, r => Assert.True(SafeLinks.IsSafe(r.Url)));
    }

    [Theory]
    [InlineData("https://docs.python.org/3/", true)]
    [InlineData("http://example.com", true)]
    [InlineData("javascript:alert(1)", false)]
    [InlineData("file:///C:/win.ini", false)]
    [InlineData("https://a.example/x\"\tfile:///C:/win.ini", false)]
    [InlineData("http://[", false)]
    public void SafeLinks_AllowOnlyPlainWebUrls(string url, bool safe) => Assert.Equal(safe, SafeLinks.IsSafe(url));
}

public sealed class CardFormatTests
{
    [Fact]
    public void Segments_ParseLightMarkdown()
    {
        Assert.Equal([("Use ", TextStyle.Plain), ("os.path.join", TextStyle.Code), (" for ", TextStyle.Plain),
                      ("portable", TextStyle.Bold), (" paths", TextStyle.Plain)],
                     CardFormat.Segments("Use `os.path.join` for **portable** paths"));
        Assert.Equal([("print(1)\n", TextStyle.Code)], CardFormat.Segments("```python\nprint(1)\n```"));
        Assert.Equal([("• one\n• two", TextStyle.Plain)], CardFormat.Segments("- one\n- two"));
    }

    [Fact]
    public void Fit_FlipsAtMonitorEdges_AndStaysInside()
    {
        var area = new System.Drawing.Rectangle(0, 0, 1920, 1040);
        Assert.Equal((100, 100), CardFormat.Fit(100, 100, 400, 200, area));
        Assert.Equal((1800 - 400 - 2 * CardFormat.CursorOffset, 100), CardFormat.Fit(1800, 100, 400, 200, area));
        Assert.Equal((100, 1000 - 200 - 2 * CardFormat.CursorOffset), CardFormat.Fit(100, 1000, 400, 200, area));
        var leftMonitor = new System.Drawing.Rectangle(-1920, 0, 1920, 1080);
        Assert.Equal((-1920, 100), CardFormat.Fit(-3000, 100, 400, 200, leftMonitor));
    }

    [Fact]
    public void Footer_ShowsThePrivacyBadgeAndCounts()
    {
        Assert.Equal("web · docs.python.org ↗ · define", CardFormat.Footer("https://docs.python.org/x", "define", 1, 1));
        Assert.Equal("local · compress · 3 in context · looked up 2×", CardFormat.Footer("", "compress", 3, 2));
    }
}

public sealed class HotkeyTests
{
    [Fact]
    public void Parse_ReadsTheSettingsFormat()
    {
        Assert.Equal((MOD_NOREPEAT | MOD_CONTROL | MOD_ALT, (uint)'H'), Hotkeys.Parse("<ctrl>+<alt>+h"));
        Assert.Equal((MOD_NOREPEAT | MOD_CONTROL | MOD_SHIFT, 0x20u), Hotkeys.Parse("<ctrl>+<shift>+<space>"));
        Assert.Equal((MOD_NOREPEAT | MOD_WIN, 0x74u), Hotkeys.Parse("<cmd>+<f5>"));
        Assert.Throws<FormatException>(() => Hotkeys.Parse("<ctrl>+<alt>"));
        Assert.Throws<FormatException>(() => Hotkeys.Parse("<ctrl>+nope"));
    }
}

public sealed class GlossaryTests : IDisposable
{
    readonly string folder = Directory.CreateTempSubdirectory("hover-glossary").FullName;

    public void Dispose()
    {
        Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools(); // release the file so it can be deleted
        Directory.Delete(folder, recursive: true);
    }

    [Fact]
    public void Add_CountsRepeatLookups_AndSkipsPassages()
    {
        using var glossary = new Glossary(Path.Combine(folder, "g.db"));
        Assert.Equal(1, glossary.Add("ephemeral", "define", "adj. short-lived", "https://m-w.com"));
        Assert.Equal(2, glossary.Add("ephemeral", "define", "adjective: lasting briefly", ""));
        Assert.Equal(1, glossary.Add("ephemeral", "translate", "efímero", ""));
        Assert.Equal(0, glossary.Add(new string('x', 200), "define", "a passage", ""));
        Assert.Equal(0, glossary.Add("term", "define", "", ""));
    }

    [Fact]
    public void ExportAnki_WritesHeaders_AndNeutralizesFormulas()
    {
        using var glossary = new Glossary(Path.Combine(folder, "g.db"));
        glossary.Add("mutex", "define", "A lock, one holder at a time.", "https://en.wikipedia.org/wiki/Mutex");
        glossary.Add("=HYPERLINK(1)", "define", "+evil", "");
        var csv = Path.Combine(folder, "cards.csv");

        Assert.Equal(2, glossary.ExportAnki(csv));
        var text = File.ReadAllText(csv);
        Assert.StartsWith("#separator:Comma\n#html:false\n#columns:Front,Back,Tags\n", text);
        Assert.Contains("mutex,\"A lock, one holder at a time.", text);
        Assert.Contains("'=HYPERLINK(1),'+evil,hover define", text);
    }
}

public sealed class ModelsTests
{
    [Fact]
    public void OpenAiMessages_TurnImagesIntoDataUrls()
    {
        var plain = JsonSerializer.Serialize(Models.OpenAiMessage(new Message("system", "s")));
        Assert.Equal("""{"role":"system","content":"s"}""", plain);

        var withImage = JsonSerializer.Serialize(Models.OpenAiMessage(new Message("user", "Screenshot", [0x89, 0x50])));
        Assert.Contains("""{"type":"text","text":"Screenshot"}""", withImage);
        Assert.Contains("data:image/png;base64,iVA=", withImage);
    }
}

public sealed class OcrTests
{
    [Fact]
    public async Task ReadText_FindsTextInAScreenshot()
    {
        if (Windows.Media.Ocr.OcrEngine.TryCreateFromUserProfileLanguages() is null)
            return; // no OCR language pack on this machine (e.g. some CI images): nothing to test
        using var image = new System.Drawing.Bitmap(600, 80);
        using (var graphics = System.Drawing.Graphics.FromImage(image))
        {
            graphics.Clear(System.Drawing.Color.White);
            using var font = new System.Drawing.Font("Segoe UI", 28);
            graphics.DrawString("PermissionError", font, System.Drawing.Brushes.Black, 10, 10);
        }

        var text = await Ocr.ReadText(Images.Png(image));

        Assert.Contains("PermissionError", text.Replace(" ", ""));
    }
}
