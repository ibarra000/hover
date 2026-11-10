using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Shapes;
using static Hover.Native;
using Screen = System.Windows.Forms.Screen;

namespace Hover;

/// <summary>
/// Built-in region capture: freeze every screen, drag a rectangle, crop (the Text-Grab FullscreenGrab pattern, MIT).
/// One overlay window per monitor, so each can use its own DPI scale. Esc cancels.
/// </summary>
internal sealed class SnipOverlay : Window
{
    const int MinSize = 6; // ignore accidental clicks
    const double Dim = 0.45;

    readonly System.Drawing.Bitmap frozen; // this monitor's screen, in physical pixels
    readonly System.Drawing.Rectangle bounds;
    readonly Rectangle selection = new() { Stroke = new SolidColorBrush(Color.FromRgb(0x60, 0xCD, 0xFF)), StrokeThickness = 2 };
    readonly Image bright = new() { Stretch = Stretch.Fill };
    readonly Action<System.Drawing.Bitmap?> onFinished;
    Point? dragStart;

    static bool active; // a second snip hotkey press while snipping does nothing

    /// <summary>Show the overlays; onDone receives the cropped image as PNG (not called if cancelled).</summary>
    public static void Start(Action<byte[]> onDone)
    {
        if (active)
            return;
        active = true;
        var overlays = new List<SnipOverlay>();
        void Finished(System.Drawing.Bitmap? crop)
        {
            active = false;
            foreach (var overlay in overlays)
                overlay.Close();
            if (crop is not null)
            {
                using (crop)
                    onDone(Images.Png(crop));
            }
        }

        foreach (var screen in Screen.AllScreens)
            overlays.Add(new SnipOverlay(screen.Bounds, Finished));
        foreach (var overlay in overlays)
            overlay.Show();
        overlays[0].Activate();
    }

    SnipOverlay(System.Drawing.Rectangle screenBounds, Action<System.Drawing.Bitmap?> onFinished)
    {
        bounds = screenBounds;
        frozen = Images.Grab(bounds);
        this.onFinished = onFinished;

        WindowStyle = WindowStyle.None;
        ResizeMode = ResizeMode.NoResize;
        ShowInTaskbar = false;
        Topmost = true;
        Cursor = Cursors.Cross;

        var screenshot = ToImageSource(frozen);
        bright.Source = screenshot;
        bright.Clip = new RectangleGeometry(Rect.Empty); // the selection shows at full brightness
        var canvas = new Canvas();
        canvas.Children.Add(selection);
        Content = new Grid
        {
            Children =
            {
                new Image { Source = screenshot, Stretch = Stretch.Fill },
                new Border { Background = Brushes.Black, Opacity = Dim },
                bright,
                canvas,
            },
        };

        // Cover exactly this monitor, in physical pixels. Moving onto a monitor with another DPI scale makes WPF
        // resize the window, so the bounds are applied again after that.
        SourceInitialized += (_, _) => CoverMonitor();
        DpiChanged += (_, _) => Dispatcher.BeginInvoke(CoverMonitor);
        KeyDown += (_, e) =>
        {
            if (e.Key == Key.Escape)
                onFinished(null);
        };
        Closed += (_, _) => frozen.Dispose();
    }

    void CoverMonitor()
    {
        var handle = new WindowInteropHelper(this).Handle;
        SetWindowPos(handle, IntPtr.Zero, bounds.X, bounds.Y, bounds.Width, bounds.Height, SWP_NOZORDER);
    }

    protected override void OnMouseLeftButtonDown(MouseButtonEventArgs e)
    {
        dragStart = e.GetPosition(this);
        CaptureMouse();
    }

    protected override void OnMouseMove(MouseEventArgs e)
    {
        if (dragStart is not { } start)
            return;
        var area = new Rect(start, e.GetPosition(this));
        Canvas.SetLeft(selection, area.X);
        Canvas.SetTop(selection, area.Y);
        selection.Width = area.Width;
        selection.Height = area.Height;
        bright.Clip = new RectangleGeometry(area);
    }

    protected override void OnMouseLeftButtonUp(MouseButtonEventArgs e)
    {
        if (dragStart is not { } start)
            return;
        ReleaseMouseCapture();
        var area = new Rect(start, e.GetPosition(this));

        // Window units -> screenshot pixels (whatever this monitor's scale is).
        var scale = frozen.Width / ActualWidth;
        var pixels = new System.Drawing.Rectangle((int)(area.X * scale), (int)(area.Y * scale),
                                                  (int)(area.Width * scale), (int)(area.Height * scale));
        pixels.Intersect(new System.Drawing.Rectangle(0, 0, frozen.Width, frozen.Height));
        var bigEnough = pixels.Width >= MinSize && pixels.Height >= MinSize;
        onFinished(bigEnough ? frozen.Clone(pixels, frozen.PixelFormat) : null);
    }

    static BitmapImage ToImageSource(System.Drawing.Bitmap bitmap)
    {
        using var stream = new MemoryStream();
        bitmap.Save(stream, System.Drawing.Imaging.ImageFormat.Bmp);
        stream.Position = 0;
        var image = new BitmapImage();
        image.BeginInit();
        image.CacheOption = BitmapCacheOption.OnLoad;
        image.StreamSource = stream;
        image.EndInit();
        image.Freeze();
        return image;
    }
}
