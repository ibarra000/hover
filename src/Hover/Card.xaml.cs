using System.Diagnostics;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;
using static Hover.Native;

namespace Hover;

/// <summary>
/// The card: a borderless, always-on-top window at the cursor that streams answers and takes follow-ups.
/// Esc, clicking elsewhere, or the timer hides it unless pinned. Shift+Tab switches mode. Drag it by any empty area;
/// pinned, it becomes a normal window (title bar, Snap, Snap Layouts, resizing).
/// </summary>
public partial class Card : Window
{
    public const string Loading = "Looking up…";
    const int MaxTitleChars = 70;
    static readonly TimeSpan ListenTime = TimeSpan.FromSeconds(10); // voice typing may take the focus for a moment
    static readonly Brush LocalGreen = new SolidColorBrush(Color.FromRgb(0x6C, 0xCB, 0x5F));
    static readonly FontFamily CodeFont = new("Cascadia Mono");
    static readonly Brush CodeBackground = new SolidColorBrush(Color.FromRgb(0x2B, 0x2B, 0x2B));

    /// <summary>Numbers each lookup shown, so a late answer for an older one is ignored.</summary>
    public int Request { get; private set; }
    public string Answer { get; private set; } = "";
    public bool Pinned { get; private set; }
    public TimeSpan HideAfter { get; set; } = TimeSpan.FromSeconds(30);

    public event Action<string>? FollowUp;
    public event Action<string>? ReplaceRequested;
    public event Action? Hidden;
    public event Action? CycleMode;

    string link = "";
    bool canReplace;
    bool moved;             // the user dragged it: stop following the cursor
    bool quitting;          // Hover is exiting: really close instead of hiding
    DateTime keepOpenUntil; // ignore focus loss until then (voice typing, pin switching)
    (int X, int Y) anchor;
    readonly DispatcherTimer hideTimer = new();

    public Card()
    {
        InitializeComponent();
        hideTimer.Tick += (_, _) => HideCard();
        SourceInitialized += (_, _) => ApplyWindowChrome();
        SizeChanged += (_, _) => PlaceNearCursor();
        Deactivated += (_, _) => HideIfNotNeeded();
        Closing += (_, closing) =>
        {
            if (quitting)
                return;
            closing.Cancel = true; // the pinned window's X button hides the card; Hover keeps running
            HideCard();
        };
    }

    public void CloseForGood()
    {
        quitting = true;
        Close();
    }

    // --- what the app calls ----------------------------------------------------------------------------------

    /// <summary>Show the card for a new lookup and return its request number (pass it to Append and Finish).</summary>
    public int ShowLookup(string title, bool keepPosition, string status = "")
    {
        Request++;
        Answer = link = "";
        canReplace = false;
        TitleText.Text = title.Length > MaxTitleChars ? title[..MaxTitleChars] + "…" : title;
        SetStatus(status, (Brush)FindResource("Muted"));
        if (!keepPosition)
        {
            var cursor = CursorPosition();
            anchor = (cursor.X + CardFormat.CursorOffset, cursor.Y + CardFormat.CursorOffset);
            moved = false;
        }
        WriteBody([(Loading, TextStyle.Plain)], muted: true);
        Show();
        Activate();
        FollowUpBox.Focus();
        PlaceNearCursor();
        RestartHideTimer();
        return Request;
    }

    /// <summary>Stream a piece of the answer in (ignored if a newer lookup replaced this one).</summary>
    public void Append(int request, string chunk)
    {
        if (request != Request || chunk.Length == 0)
            return;
        if (Answer.Length == 0)
            WriteBody([], muted: false); // drop "Looking up…"
        Answer += chunk;
        CurrentParagraph().Inlines.Add(new Run(chunk));
        RestartHideTimer(); // a long answer shouldn't vanish mid-stream
    }

    /// <summary>Show the complete answer with formatting, plus the footer. Error text is never offered for Replace.</summary>
    public void Finish(int request, string text, string footer, string sourceLink, bool replaceable = true)
    {
        if (request != Request)
            return;
        Answer = text;
        link = sourceLink;
        canReplace = replaceable && text.Length > 0 && footer != "error";
        if (text.Length > 0)
            WriteBody(CardFormat.Segments(text), muted: false);
        else
            WriteBody([("(empty answer)", TextStyle.Plain)], muted: true);

        var color = link.Length > 0 ? (Brush)FindResource("Accent")
                  : footer.StartsWith("local") ? LocalGreen
                  : (Brush)FindResource("Muted");
        SetStatus(footer, color);
        RestartHideTimer();
    }

    /// <summary>A message from Hover itself (never pasted into the user's document by Replace).</summary>
    public void Notice(string title, string text)
    {
        var request = ShowLookup(title, keepPosition: false);
        Finish(request, text, "", "", replaceable: false);
    }

    public void HideCard()
    {
        hideTimer.Stop();
        Hide();
        Hidden?.Invoke();
    }

    // --- buttons and keys ------------------------------------------------------------------------------------

    void OnCopy(object sender, RoutedEventArgs e) => CopyAnswer();
    void OnReplace(object sender, RoutedEventArgs e) => ReplaceSelection();
    void OnPin(object sender, RoutedEventArgs e) => TogglePin();
    void OnMic(object sender, RoutedEventArgs e) => Dictate();

    protected override void OnPreviewKeyDown(KeyEventArgs e)
    {
        base.OnPreviewKeyDown(e);
        RestartHideTimer(); // don't hide while the user is typing
        e.Handled = RunShortcut(e.Key, Keyboard.Modifiers);
    }

    /// <summary>The card's keyboard shortcuts. Returns false for any other key, which then works normally.</summary>
    bool RunShortcut(Key key, ModifierKeys modifiers)
    {
        var ctrl = modifiers == ModifierKeys.Control;
        if (key == Key.Escape)
            HideCard();
        else if (key == Key.Tab && modifiers == ModifierKeys.Shift)
            CycleMode?.Invoke();
        else if (ctrl && key == Key.C && !HasSelectionInCard()) // with a selection, Ctrl+C copies just that
            CopyAnswer();
        else if (ctrl && key == Key.R)
            ReplaceSelection();
        else if (ctrl && key == Key.M)
            Dictate();
        else if (ctrl && key == Key.P)
            TogglePin();
        else
            return false;
        return true;
    }

    void OnFollowUpKey(object sender, KeyEventArgs e)
    {
        var question = FollowUpBox.Text.Trim();
        if (e.Key != Key.Enter || question.Length == 0)
            return;
        FollowUpBox.Clear();
        FollowUp?.Invoke(question);
    }

    void OnStatusClick(object sender, MouseButtonEventArgs e)
    {
        if (SafeLinks.IsSafe(link))
            Process.Start(new ProcessStartInfo(link) { UseShellExecute = true });
    }

    /// <summary>A press on an empty area (anything that doesn't handle the mouse itself) drags the window.</summary>
    protected override void OnMouseLeftButtonDown(MouseButtonEventArgs e)
    {
        base.OnMouseLeftButtonDown(e);
        if (e.OriginalSource == Status || e.ButtonState != MouseButtonState.Pressed)
            return; // the footer link opens the source instead; DragMove needs the button still down
        moved = true; // stay where the user drops it while the answer keeps streaming
        DragMove();
    }

    void CopyAnswer()
    {
        Clipboard.SetText(Answer);
        SetStatus("copied", (Brush)FindResource("Muted"));
    }

    void ReplaceSelection()
    {
        if (!canReplace)
            return;
        HideCard();
        ReplaceRequested?.Invoke(Answer);
    }

    /// <summary>Say a follow-up: Windows voice typing fills the box, Enter sends it.</summary>
    void Dictate()
    {
        FollowUpBox.Focus();
        keepOpenUntil = DateTime.Now + ListenTime;
        Capture.StartDictation();
        RestartHideTimer();
    }

    /// <summary>Pinned: a normal window that Windows Snap can arrange. Unpinned: the borderless card.</summary>
    public void TogglePin()
    {
        Pinned = !Pinned;
        PinButton.Content = Pinned ? "Unpin" : "Pin";
        keepOpenUntil = DateTime.Now.AddSeconds(1); // switching the frame briefly moves the focus
        moved = true;
        WindowStyle = Pinned ? WindowStyle.SingleBorderWindow : WindowStyle.None;
        ResizeMode = Pinned ? ResizeMode.CanResize : ResizeMode.NoResize;
        Topmost = !Pinned;
        ShowInTaskbar = Pinned;
        if (!Pinned)
            SizeToContent = SizeToContent.Height; // forget a size the user dragged or snapped: fit the answer again
        Activate();
        RestartHideTimer();
    }

    // --- helpers ---------------------------------------------------------------------------------------------

    void HideIfNotNeeded()
    {
        if (!Pinned && DateTime.Now > keepOpenUntil && IsVisible)
            HideCard();
    }

    bool HasSelectionInCard()
    {
        return (FollowUpBox.IsKeyboardFocused && FollowUpBox.SelectionLength > 0)
               || (Body.IsKeyboardFocusWithin && !Body.Selection.IsEmpty);
    }

    void SetStatus(string text, Brush color)
    {
        Status.Text = text;
        Status.Foreground = color;
        Status.Cursor = link.Length > 0 ? Cursors.Hand : null;
    }

    void WriteBody(IEnumerable<(string Text, TextStyle Style)> runs, bool muted)
    {
        var paragraph = new Paragraph { Margin = new Thickness(0) };
        foreach (var (text, style) in runs)
        {
            var run = new Run(text);
            if (muted)
                run.Foreground = (Brush)FindResource("Muted");
            if (style == TextStyle.Bold)
                run.FontWeight = FontWeights.SemiBold;
            if (style == TextStyle.Code)
            {
                run.FontFamily = CodeFont;
                run.Background = CodeBackground;
            }
            paragraph.Inlines.Add(run);
        }
        Body.Document.Blocks.Clear();
        Body.Document.Blocks.Add(paragraph);
    }

    Paragraph CurrentParagraph() => (Paragraph)Body.Document.Blocks.LastBlock;

    void RestartHideTimer()
    {
        hideTimer.Stop();
        if (Pinned)
            return;
        hideTimer.Interval = HideAfter;
        hideTimer.Start();
    }

    /// <summary>Put the card next to the cursor, inside that monitor's work area (physical pixels throughout).</summary>
    void PlaceNearCursor()
    {
        if (Pinned || moved || !IsVisible)
            return;
        var handle = new WindowInteropHelper(this).Handle;
        var scale = PresentationSource.FromVisual(this)?.CompositionTarget.TransformToDevice.M11 ?? 1.0;
        var width = (int)(ActualWidth * scale);
        var height = (int)(ActualHeight * scale);
        var workArea = System.Windows.Forms.Screen.FromPoint(new System.Drawing.Point(anchor.X, anchor.Y)).WorkingArea;
        var (x, y) = CardFormat.Fit(anchor.X, anchor.Y, width, height, workArea);
        SetWindowPos(handle, IntPtr.Zero, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
    }

    /// <summary>Windows 11 rounded corners, grey border, and a dark title bar when pinned (ignored on Windows 10).</summary>
    void ApplyWindowChrome()
    {
        var handle = new WindowInteropHelper(this).Handle;
        SetWindowAttribute(handle, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND);
        SetWindowAttribute(handle, DWMWA_BORDER_COLOR, 0x3A3A3A);
        SetWindowAttribute(handle, DWMWA_USE_IMMERSIVE_DARK_MODE, 1);
    }
}
