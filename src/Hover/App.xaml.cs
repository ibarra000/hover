using System.Diagnostics;
using System.IO;
using System.Text.Json.Nodes;
using System.Windows;
using Microsoft.Win32;
using Forms = System.Windows.Forms;

namespace Hover;

/// <summary>
/// Wiring: the tray icon, global hotkeys, and the capture -> answer -> card flow.
/// Everything runs on the UI thread; slow work is awaited (network) or moved off it with Task.Run (key presses).
/// </summary>
public partial class App : Application
{
    const string EmptyMessage = "Context cleared. Select text, copy something, or snip (Ctrl+Alt+S), then press the hotkey.";
    const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    static readonly string GlossaryFile = Path.Combine(Settings.DataDir, "glossary.db");
    static readonly string ExportFile = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "Hover glossary.csv");

    readonly Session session = new(Models.Stream, WebSearches.DuckDuckGoThenWikipedia);
    // Windows can only be created once the Application exists, so these are set in OnStartup.
    Glossary glossary = null!;
    Card card = null!;
    Hotkeys hotkeys = null!;
    Forms.NotifyIcon tray = null!;

    CancellationTokenSource cancel = new(); // cancels the answer that's currently streaming
    IntPtr window;                          // the app the user was in, for Replace
    (Item Item, string Mode)? current;      // what the card shows (not follow-ups): Shift+Tab re-answers it

    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        var (settings, startupError) = Settings.SafeLoad();
        glossary = new Glossary(GlossaryFile);
        hotkeys = new Hotkeys();
        WinClipboard.Owner = hotkeys.WindowHandle;
        card = new Card();
        card.FollowUp += FollowUp;
        card.ReplaceRequested += Replace;
        card.Hidden += () => cancel.Cancel();
        card.CycleMode += CycleMode;
        tray = CreateTray();
        var problems = RegisterHotkeys(settings);
        if (startupError.Length > 0)
            problems.Insert(0, startupError);
        if (problems.Count > 0)
            card.Notice("Hover", string.Join("\n", problems));

        var warmUpError = await Models.WarmUp(settings, Images.Blank());
        if (warmUpError.Length > 0)
            card.Notice("Hover", warmUpError);
    }

    // --- hotkeys ---------------------------------------------------------------------------------------------

    List<string> RegisterHotkeys(Settings settings)
    {
        var actions = new Dictionary<string, Action>
        {
            ["lookup"] = () => Lookup(null),
            ["hover"] = Hover,
            ["snip"] = Snip,
        };
        var problems = new List<string>();
        foreach (var (name, combo) in settings.Hotkeys)
        {
            if (actions.TryGetValue(name, out var action) && !string.IsNullOrEmpty(combo))
                problems.Add(hotkeys.Add(combo, () => Guarded(action)));
        }
        foreach (var (name, mode) in settings.Modes)
        {
            if (!string.IsNullOrEmpty(mode.Hotkey))
                problems.Add(hotkeys.Add(mode.Hotkey, () => Guarded(() => Lookup(name))));
        }
        return problems.Where(problem => problem.Length > 0).ToList();
    }

    /// <summary>Selection first, then the clipboard (text or image). Nothing at all clears the context.</summary>
    void Lookup(string? mode)
    {
        CaptureThenAsk(mode, async settings =>
        {
            var selection = await Capture.SelectedText(settings);
            return selection.Length > 0 ? new TextItem(selection) : Capture.ClipboardItem(settings.MaxChars);
        });
    }

    /// <summary>Explain what's under the mouse: its text, or OCR of the area around the cursor.</summary>
    void Hover() => CaptureThenAsk(null, settings => Capture.UnderCursor(settings.MaxChars));

    void Snip()
    {
        window = Native.GetForegroundWindow();
        if (FreshSettings() is not { } settings)
            return;
        card.HideCard(); // keep the card out of the frozen screenshot
        SnipOverlay.Start(png => Ask(new Shot(png), null, settings));
    }

    /// <summary>Capture off the UI thread (it presses keys and waits for the clipboard), then ask.</summary>
    async void CaptureThenAsk(string? mode, Func<Settings, Task<Item?>> grab)
    {
        window = Native.GetForegroundWindow();
        if (FreshSettings() is not { } settings)
            return;
        Item? item;
        try
        {
            item = await Task.Run(() => grab(settings));
        }
        catch (Exception error) // clipboard locked by another app, screen grab refused, ...: say so in the card
        {
            card.Notice("Hover", $"Couldn't read the selection or clipboard: {error.Message}");
            return;
        }
        Ask(item, mode, settings);
    }

    /// <summary>Run a hotkey or menu action; if it fails, show why in the card instead of crashing the tray app.</summary>
    void Guarded(Action action)
    {
        try
        {
            action();
        }
        catch (Exception error)
        {
            card.Notice("Hover", $"Something went wrong: {error.Message}");
        }
    }

    // --- the card's actions ----------------------------------------------------------------------------------

    void FollowUp(string question)
    {
        if (FreshSettings() is { } settings)
            Ask(new TextItem(question), null, settings, keepPosition: true, isFollowUp: true);
    }

    /// <summary>Shift+Tab in the card: make the next mode the default and answer the same item again in it.</summary>
    void CycleMode()
    {
        if (FreshSettings() is not { } settings)
            return;
        var names = settings.Modes.Keys.ToList();
        var shown = current?.Mode ?? settings.Mode;
        var position = names.IndexOf(shown); // -1 if the mode was removed from settings.json: start from the first
        var next = names[(position + 1) % names.Count];
        Settings.Update("mode", next);

        if (current is { } lookup)
            Ask(lookup.Item, next, settings with { Mode = next }, keepPosition: true);
        else
            card.Notice("Hover", $"Mode: {next}");
    }

    async void Replace(string text)
    {
        string problem;
        try
        {
            var pasted = await Task.Run(() => Capture.PasteText(text, window));
            problem = pasted ? "" : "Couldn't paste safely (the original window is gone, or you've copied files).";
        }
        catch (IOException error) // the clipboard stayed locked by another app
        {
            problem = $"Couldn't paste: {error.Message}.";
        }
        if (problem.Length > 0)
            card.Notice("Hover", $"{problem} Use Copy instead.");
    }

    // --- the lookup flow -------------------------------------------------------------------------------------

    /// <summary>Re-read settings.json so edits apply immediately; a broken file shows its error instead.</summary>
    Settings? FreshSettings()
    {
        var (settings, error) = Settings.SafeLoad();
        if (error.Length == 0)
            return settings;
        card.Notice("Hover", error);
        return null;
    }

    async void Ask(Item? item, string? mode, Settings settings, bool keepPosition = false, bool isFollowUp = false)
    {
        if (item is null || item is TextItem { Text: "" })
        {
            ClearContext();
            return;
        }

        if (item is TextItem text) // ">>translate: bonjour" picks the mode for this one item
        {
            var (inlineMode, rest) = Session.SplitInlineMode(text.Text, settings.Modes);
            item = new TextItem(rest);
            mode = inlineMode ?? mode;
        }
        mode ??= settings.Mode;
        if (!isFollowUp)
            current = (item, mode); // without the ">>mode:" prefix, so Shift+Tab can change the mode

        var (request, token) = StartCard(item, mode, settings, keepPosition);
        try
        {
            if (item is Shot { OcrText: "" } shot)
                item = shot with { OcrText = await Ocr.ReadText(shot.Png) };
            var answer = await session.AskAsync(item, settings, mode, chunk => card.Append(request, chunk), token);
            if (!answer.Cancelled)
                ShowAnswer(request, item, answer, settings, isFollowUp);
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested)
        {
            // The card was closed or a newer lookup started: nothing to show.
        }
        catch (Exception error)
        {
            // Ollama down, model missing, server timeout, OCR failure, ...: show it in the card, don't swallow it.
            card.Finish(request, $"Error: {error.Message}", "error", "");
        }
    }

    void ClearContext()
    {
        cancel.Cancel();
        session.Clear();
        current = null;
        card.Notice("Hover", EmptyMessage);
    }

    /// <summary>Cancel whatever was still streaming and show the card for this lookup.</summary>
    (int Request, CancellationToken Token) StartCard(Item item, string mode, Settings settings, bool keepPosition)
    {
        cancel.Cancel();
        cancel = new CancellationTokenSource();
        var title = item is TextItem text ? text.Text : "Screenshot";
        card.HideAfter = TimeSpan.FromSeconds(settings.HideAfterSeconds);
        var request = card.ShowLookup(title, keepPosition, $"{mode} · Shift+Tab: next mode");
        return (request, cancel.Token);
    }

    void ShowAnswer(int request, Item item, Answer answer, Settings settings, bool isFollowUp)
    {
        var timesLookedUp = isFollowUp ? 0 : Remember(item, answer, settings); // questions aren't glossary terms
        var footer = CardFormat.Footer(answer.Link, answer.Mode, session.Turns, timesLookedUp);
        card.Finish(request, answer.Text, footer, answer.Link);
    }

    /// <summary>Add a looked-up term to the glossary; a database problem never costs the user their answer.</summary>
    int Remember(Item item, Answer answer, Settings settings)
    {
        if (!settings.Glossary || item is not TextItem text)
            return 0;
        try
        {
            return glossary.Add(text.Text, answer.Mode, answer.Text, answer.Link);
        }
        catch (Microsoft.Data.Sqlite.SqliteException)
        {
            return 0;
        }
    }

    // --- tray ------------------------------------------------------------------------------------------------

    Forms.NotifyIcon CreateTray()
    {
        var menu = new Forms.ContextMenuStrip();
        menu.Opening += (_, _) => FillTrayMenu(menu); // rebuilt each time, so it always matches settings.json
        using var icon = GetResourceStream(new Uri("pack://application:,,,/hover.ico")).Stream;
        return new Forms.NotifyIcon
        {
            Icon = new System.Drawing.Icon(icon),
            Text = "Hover",
            ContextMenuStrip = menu,
            Visible = true,
        };
    }

    void FillTrayMenu(Forms.ContextMenuStrip menu)
    {
        var (settings, _) = Settings.SafeLoad();
        menu.Items.Clear();
        foreach (var name in settings.Modes.Keys)
            AddMenuItem(menu, char.ToUpper(name[0]) + name[1..], settings.Mode == name, () => Settings.Update("mode", name));
        menu.Items.Add(new Forms.ToolStripSeparator());
        AddMenuItem(menu, "Strict offline", settings.Offline, () => Settings.Update("offline", !settings.Offline));
        AddMenuItem(menu, "Start with Windows", StartsWithWindows(), () => SetStartWithWindows(!StartsWithWindows()));
        AddMenuItem(menu, "Clear context", false, session.Clear);
        AddMenuItem(menu, "Export glossary to Anki", false, ExportGlossary);
        AddMenuItem(menu, "Settings", false, () => Process.Start(new ProcessStartInfo(Settings.FilePath) { UseShellExecute = true }));
        AddMenuItem(menu, "Quit", false, Quit);
    }

    void AddMenuItem(Forms.ContextMenuStrip menu, string text, bool isChecked, Action onClick)
    {
        menu.Items.Add(new Forms.ToolStripMenuItem(text, null, (_, _) => Guarded(onClick)) { Checked = isChecked });
    }

    static bool StartsWithWindows()
    {
        using var key = Registry.CurrentUser.OpenSubKey(RunKey);
        return key?.GetValue("Hover") is not null;
    }

    static void SetStartWithWindows(bool enabled)
    {
        using var key = Registry.CurrentUser.CreateSubKey(RunKey);
        if (enabled)
            key.SetValue("Hover", $"\"{Environment.ProcessPath}\"");
        else
            key.DeleteValue("Hover", throwOnMissingValue: false);
    }

    void ExportGlossary()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(ExportFile)!); // e.g. a OneDrive-redirected Documents folder
            var cards = glossary.ExportAnki(ExportFile);
            Process.Start("explorer", $"/select,\"{ExportFile}\"");
            card.Notice("Glossary exported", $"{cards} cards → {ExportFile}");
        }
        catch (Exception error) when (error is IOException or Microsoft.Data.Sqlite.SqliteException)
        {
            card.Notice("Hover", $"Couldn't export the glossary: {error.Message}");
        }
    }

    void Quit()
    {
        hotkeys.Dispose();
        glossary.Dispose();
        tray.Dispose();
        card.CloseForGood();
        Shutdown();
    }
}
