using System.Diagnostics;
using System.IO;
using System.Net.Http;

namespace Hover.Tests;

/// <summary>Against the real web and a running Ollama. Run with: dotnet test --filter Category=Live</summary>
[Trait("Category", "Live")]
public sealed class LiveTests
{
    [Fact]
    public async Task WebSearch_ReturnsResults_EvenWhenDuckDuckGoIsRateLimiting()
    {
        var results = await WebSearches.DuckDuckGoThenWikipedia("ephemeral", 3, CancellationToken.None);
        Console.WriteLine(string.Join(Environment.NewLine, results.Select(r => $"{r.Title} <{r.Url}>")));
        Assert.Equal(3, results.Count);
        Assert.All(results, r => Assert.True(SafeLinks.IsSafe(r.Url)));
    }

    [Fact]
    public async Task Ollama_StreamsAGroundedAnswer_AndAScreenshotAnswer()
    {
        var settings = new Settings();
        var session = new Session(Models.Stream, WebSearches.DuckDuckGoThenWikipedia);
        var firstToken = TimeSpan.Zero;
        var clock = Stopwatch.StartNew();

        var answer = await session.AskAsync(new TextItem("ephemeral"), settings, null,
                                            _ => firstToken = firstToken == TimeSpan.Zero ? clock.Elapsed : firstToken,
                                            CancellationToken.None);

        Assert.False(string.IsNullOrWhiteSpace(answer.Text));
        Console.WriteLine($"text: first token {firstToken.TotalSeconds:F1}s, total {clock.Elapsed.TotalSeconds:F1}s, " +
                          $"link {answer.Link}\n{answer.Text}");

        clock.Restart();
        using var image = new System.Drawing.Bitmap(700, 80);
        using (var graphics = System.Drawing.Graphics.FromImage(image))
        {
            graphics.Clear(System.Drawing.Color.White);
            using var font = new System.Drawing.Font("Consolas", 18);
            graphics.DrawString("PermissionError: [Errno 13] C:\\data\\out.csv", font, System.Drawing.Brushes.Black, 8, 20);
        }
        var png = Images.Png(image);
        var shot = await session.AskAsync(new Shot(png, await Ocr.ReadText(png)), settings, "explain", _ => { },
                                          CancellationToken.None);
        Console.WriteLine($"screenshot: {clock.Elapsed.TotalSeconds:F1}s\n{shot.Text}");
        Assert.Contains("ermission", shot.Text);
    }
}

/// <summary>Shows the real card on screen and saves screenshots to %TEMP% (look at them to check the design).</summary>
[Trait("Category", "Live")]
public sealed class CardScreenshotTests
{
    [Fact]
    public void Card_RendersStreamedMarkdown_AndPinsAsANormalWindow()
    {
        Exception? failure = null;
        var thread = new Thread(() =>
        {
            try
            {
                var card = new Card();
                var request = card.ShowLookup("ephemeral", keepPosition: false, status: "define · Shift+Tab: next mode");
                card.Append(request, "adjective: ");
                card.Finish(request, "**adjective**: lasting a very short time. Use `ephemeral` storage for scratch data.\n- from Greek *ephēmeros*",
                            "web · en.wikipedia.org ↗ · define", "https://en.wikipedia.org/wiki/Ephemerality");
                Save(card, "hover-card.png");
                card.TogglePin();
                Assert.Equal(System.Windows.WindowStyle.SingleBorderWindow, card.WindowStyle);
                Assert.False(card.Topmost);
                Save(card, "hover-card-pinned.png");
                card.TogglePin();
                Assert.Equal(System.Windows.WindowStyle.None, card.WindowStyle);
                card.HideCard();
            }
            catch (Exception error)
            {
                failure = error;
            }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        thread.Join();
        if (failure is not null)
            throw failure;
    }

    static void Save(Card card, string name)
    {
        for (var i = 0; i < 20; i++) // let WPF lay out and paint
        {
            System.Windows.Threading.Dispatcher.CurrentDispatcher.Invoke(() => { },
                System.Windows.Threading.DispatcherPriority.ApplicationIdle);
            Thread.Sleep(25);
        }
        var handle = new System.Windows.Interop.WindowInteropHelper(card).Handle;
        GetWindowRect(handle, out var rect);
        var area = System.Drawing.Rectangle.FromLTRB(rect.Left - 30, rect.Top - 30, rect.Right + 30, rect.Bottom + 30);
        using var image = Images.Grab(area);
        image.Save(Path.Combine(Path.GetTempPath(), name));
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    static extern bool GetWindowRect(IntPtr window, out Rect rect);

    struct Rect
    {
        public int Left, Top, Right, Bottom;
    }
}
