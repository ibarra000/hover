using System.Diagnostics;
using System.Windows.Automation;
using System.Windows.Automation.Text;
using static Hover.Native;

namespace Hover;

/// <summary>
/// Getting the user's text without making them copy it: UI Automation first, a clipboard-preserving copy second.
/// Ported from selection-hook's WINDOWS.md (UIA, then Ctrl+Insert, then Ctrl+C, detected by the clipboard sequence
/// number, with backup and restore) and PowerToys (release held modifiers before sending keys). Both MIT.
/// </summary>
internal static class Capture
{
    static readonly TimeSpan UiaTimeout = TimeSpan.FromMilliseconds(400);
    static readonly TimeSpan HoverTimeout = TimeSpan.FromMilliseconds(800);
    const int CopyWaitMs = 400, ModifierWaitMs = 500;
    const int HoverHalfWidth = 300, HoverHalfHeight = 100; // the OCR crop around the cursor

    static readonly HashSet<string> Terminals =
    [
        "windowsterminal", "openconsole", "conhost", "cmd", "powershell", "pwsh", "wezterm-gui", "alacritty", "mintty",
    ];
    static readonly HashSet<ControlType> GenericControls =
        [ControlType.Pane, ControlType.Window, ControlType.Document, ControlType.Group, ControlType.Custom];
    static readonly byte[] Modifiers = [VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN];

    // One synthetic copy or paste at a time, or the backups would capture each other's temporary text.
    static readonly SemaphoreSlim ClipboardInUse = new(1, 1);

    // --- what to look up -------------------------------------------------------------------------------------

    /// <summary>The highlighted text in the focused app (at most maxChars), or "" if nothing is selected.</summary>
    public static async Task<string> SelectedText(Settings settings)
    {
        var text = await WithTimeout(() => UiaSelection(settings.MaxChars), UiaTimeout);
        if (text.Length > 0 || !settings.CopyFallback)
            return text;
        // Ctrl+C is opt-in, and never sent to a terminal: there it would interrupt the running program.
        var allowCtrlC = settings.CtrlCFallback && !Terminals.Contains(ForegroundProcess());
        var copied = await CopySelection(allowCtrlC);
        return copied.Length > settings.MaxChars ? copied[..settings.MaxChars] : copied;
    }

    /// <summary>The clipboard's text (whitespace collapsed), else a copied image, else null.</summary>
    public static Item? ClipboardItem(int maxChars)
    {
        // Text wins: Excel, Word and PowerPoint put both text and a picture on the clipboard.
        var text = OneLine(WinClipboard.Text());
        if (text.Length > 0)
            return new TextItem(text.Length > maxChars ? text[..maxChars] : text);
        return WinClipboard.Image() is { } png ? new Shot(png) : null;
    }

    /// <summary>The text under the mouse, else a screenshot of the area around it.</summary>
    public static async Task<Item?> UnderCursor(int maxChars)
    {
        var text = await WithTimeout(() => UiaTextAtCursor(maxChars), HoverTimeout);
        if (text.Length > 0)
            return new TextItem(text);
        var cursor = CursorPosition();
        var area = new System.Drawing.Rectangle(cursor.X - HoverHalfWidth, cursor.Y - HoverHalfHeight,
                                                2 * HoverHalfWidth, 2 * HoverHalfHeight);
        using var image = Images.Grab(area);
        return new Shot(Images.Png(image));
    }

    /// <summary>All whitespace runs (including newlines) collapsed to single spaces.</summary>
    static string OneLine(string text) => string.Join(" ", text.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));

    // --- UI Automation ---------------------------------------------------------------------------------------

    /// <summary>Run a UI Automation query with a time limit: "" if it fails or hangs (busy apps can hang UIA).</summary>
    static async Task<string> WithTimeout(Func<string> query, TimeSpan limit)
    {
        var task = Task.Run(() =>
        {
            try
            {
                return query();
            }
            catch (Exception error) when (error is ElementNotAvailableException or InvalidOperationException
                                                   or System.Runtime.InteropServices.COMException)
            {
                return ""; // apps without accessibility support
            }
        });
        var finished = await Task.WhenAny(task, Task.Delay(limit));
        return finished == task ? task.Result : "";
    }

    static string UiaSelection(int maxChars)
    {
        var element = AutomationElement.FocusedElement;
        // The focused element is often a child of the one that exposes the text.
        for (var level = 0; level < 4 && element is not null; level++)
        {
            if (element.TryGetCurrentPattern(TextPattern.Pattern, out var pattern))
            {
                var ranges = ((TextPattern)pattern).GetSelection();
                var text = string.Concat(ranges.Select(range => range.GetText(maxChars))).Trim();
                return text.Length > maxChars ? text[..maxChars] : text;
            }
            element = TreeWalker.ControlViewWalker.GetParent(element);
        }
        return "";
    }

    static string UiaTextAtCursor(int maxChars)
    {
        var cursor = CursorPosition();
        var point = new System.Windows.Point(cursor.X, cursor.Y);
        var element = AutomationElement.FromPoint(point);
        if (element.TryGetCurrentPattern(TextPattern.Pattern, out var pattern))
        {
            var range = ((TextPattern)pattern).RangeFromPoint(point);
            range.ExpandToEnclosingUnit(TextUnit.Paragraph);
            var paragraph = range.GetText(maxChars).Trim();
            if (paragraph.Length > 0)
                return paragraph;
        }
        // No text pattern: a button or list item's name is still worth explaining, a generic pane's isn't.
        var name = element.Current.Name?.Trim() ?? "";
        return name.Length > 1 && !GenericControls.Contains(element.Current.ControlType) ? name : "";
    }

    // --- synthetic copy and paste ----------------------------------------------------------------------------

    /// <summary>
    /// Copy the selection with synthetic keys, read it, then put the user's clipboard back exactly as it was.
    /// Gives up ("") rather than risk the clipboard: if another copy or paste is running, or if it holds copied
    /// files or vector images, which can't be restored byte for byte.
    /// </summary>
    static async Task<string> CopySelection(bool allowCtrlC)
    {
        if (!await ClipboardInUse.WaitAsync(0))
            return "";
        try
        {
            if (WinClipboard.HasAny(WinClipboard.Unrestorable))
                return "";
            WaitForModifiersReleased();
            var backup = WinClipboard.Backup();
            var before = WinClipboard.SequenceNumber;
            try
            {
                PressWithCtrl(VK_INSERT); // copies almost everywhere, and never interrupts a terminal program
                var copied = await ClipboardChanged(before);
                if (!copied && allowCtrlC)
                {
                    PressWithCtrl((byte)'C');
                    copied = await ClipboardChanged(before);
                }
                return copied ? WinClipboard.Text().Trim() : "";
            }
            finally
            {
                // Even if reading failed, or a slow app copied late, the user gets their clipboard back.
                if (WinClipboard.SequenceNumber != before)
                    WinClipboard.Restore(backup);
            }
        }
        finally
        {
            ClipboardInUse.Release();
        }
    }

    /// <summary>
    /// Replace the selection in `window` with text, then restore the clipboard (PowerToys' approach).
    /// Returns false, pasting nothing, if that window can't be brought back to the front, or if the clipboard holds
    /// copied files or vector images (they couldn't be put back afterwards).
    /// </summary>
    public static async Task<bool> PasteText(string text, IntPtr window)
    {
        SetForegroundWindow(window);
        await Task.Delay(150);
        if (window == IntPtr.Zero || GetForegroundWindow() != window)
            return false; // never paste into whatever happens to have focus
        if (Terminals.Contains(ForegroundProcess()))
            text = OneLine(text); // a newline pasted into a shell would run the line

        await ClipboardInUse.WaitAsync();
        try
        {
            if (WinClipboard.HasAny(WinClipboard.Unrestorable))
                return false;
            var backup = WinClipboard.Backup();
            try
            {
                WinClipboard.SetText(text);
                WaitForModifiersReleased();
                PressWithCtrl((byte)'V');
                await Task.Delay(300); // let the app read the clipboard before the old contents go back
                return true;
            }
            finally
            {
                WinClipboard.Restore(backup);
            }
        }
        finally
        {
            ClipboardInUse.Release();
        }
    }

    /// <summary>Win+H: Windows' built-in voice typing, which types into whatever has focus.</summary>
    public static void StartDictation() => PressWith(VK_LWIN, (byte)'H');

    static void PressWithCtrl(byte key) => PressWith(VK_CONTROL, key);

    static void PressWith(byte modifier, byte key)
    {
        keybd_event(modifier, 0, 0, UIntPtr.Zero);
        keybd_event(key, 0, 0, UIntPtr.Zero);
        keybd_event(key, 0, KEYEVENTF_KEYUP, UIntPtr.Zero);
        keybd_event(modifier, 0, KEYEVENTF_KEYUP, UIntPtr.Zero);
    }

    /// <summary>A still-held Ctrl+Alt from the hotkey would turn our Ctrl+C into Ctrl+Alt+C (WritingTools #207).</summary>
    static void WaitForModifiersReleased()
    {
        var deadline = Environment.TickCount64 + ModifierWaitMs;
        while (Environment.TickCount64 < deadline && Modifiers.Any(key => (GetAsyncKeyState(key) & 0x8000) != 0))
            Thread.Sleep(10);
    }

    static async Task<bool> ClipboardChanged(uint before)
    {
        var deadline = Environment.TickCount64 + CopyWaitMs;
        while (Environment.TickCount64 < deadline)
        {
            if (WinClipboard.SequenceNumber != before)
                return true;
            await Task.Delay(10);
        }
        return false;
    }

    /// <summary>The focused window's program name, lowercase ("" if unreadable, e.g. an elevated app).</summary>
    public static string ForegroundProcess()
    {
        GetWindowThreadProcessId(GetForegroundWindow(), out var processId);
        try
        {
            using var process = Process.GetProcessById((int)processId);
            return process.ProcessName.ToLowerInvariant();
        }
        catch (ArgumentException)
        {
            return "";
        }
    }
}
