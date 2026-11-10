using System.Runtime.InteropServices.WindowsRuntime;
using Windows.Graphics.Imaging;
using Windows.Media.Ocr;
using Windows.Storage.Streams;

namespace Hover;

/// <summary>Local text recognition with the OCR engine built into Windows, as PowerToys and Text-Grab do.</summary>
internal static class Ocr
{
    const int SmallHeight = 60; // Windows OCR misses small text, so shorter crops are upscaled 2x

    /// <summary>Text found in a PNG, or "" (OCR is only a hint: the vision model still sees the image).</summary>
    public static async Task<string> ReadText(byte[] png)
    {
        var engine = OcrEngine.TryCreateFromUserProfileLanguages();
        if (engine is null)
            return ""; // no OCR language pack for the user's languages

        using var stream = new InMemoryRandomAccessStream();
        await stream.WriteAsync(png.AsBuffer());
        stream.Seek(0);
        var decoder = await BitmapDecoder.CreateAsync(stream);

        var transform = new BitmapTransform();
        if (decoder.PixelHeight < SmallHeight)
        {
            transform.ScaledWidth = decoder.PixelWidth * 2;
            transform.ScaledHeight = decoder.PixelHeight * 2;
        }
        using var bitmap = await decoder.GetSoftwareBitmapAsync(BitmapPixelFormat.Bgra8, BitmapAlphaMode.Premultiplied,
            transform, ExifOrientationMode.IgnoreExifOrientation, ColorManagementMode.DoNotColorManage);
        var result = await engine.RecognizeAsync(bitmap);
        return string.Join("\n", result.Lines.Select(line => line.Text)).Trim();
    }
}
