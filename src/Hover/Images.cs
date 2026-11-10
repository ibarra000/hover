using System.Drawing;
using System.Drawing.Imaging;
using System.IO;

namespace Hover;

/// <summary>Screen grabs and PNG conversion. Every image sent to the vision model fits in MaxPixels (big snips stay fast).</summary>
internal static class Images
{
    const int MaxPixels = 1280;

    public static byte[] Png(Bitmap image)
    {
        var scale = Math.Min(1.0, (double)MaxPixels / Math.Max(image.Width, image.Height));
        using var resized = scale < 1 ? new Bitmap(image, (int)(image.Width * scale), (int)(image.Height * scale)) : null;
        using var stream = new MemoryStream();
        (resized ?? image).Save(stream, ImageFormat.Png);
        return stream.ToArray();
    }

    public static byte[] Shrink(byte[] png)
    {
        using var stream = new MemoryStream(png);
        using var image = new Bitmap(stream);
        return Png(image);
    }

    /// <summary>A screenshot of a rectangle of the desktop, in physical pixels (any monitor).</summary>
    public static Bitmap Grab(Rectangle area)
    {
        var image = new Bitmap(area.Width, area.Height);
        try
        {
            using var graphics = Graphics.FromImage(image);
            graphics.CopyFromScreen(area.Location, Point.Empty, area.Size);
            return image;
        }
        catch
        {
            image.Dispose(); // e.g. the secure desktop (UAC prompt) can't be captured
            throw;
        }
    }

    public static byte[] Blank()
    {
        using var image = new Bitmap(64, 64);
        return Png(image);
    }
}
