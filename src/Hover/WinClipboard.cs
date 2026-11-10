using System.IO;
using System.Runtime.InteropServices;
using static Hover.Native;

namespace Hover;

/// <summary>
/// Direct access to the Windows clipboard, so it can be backed up and restored format by format.
/// Approach from PowerToys' ClipboardHelper.cs and selection-hook's WINDOWS.md (both MIT).
/// </summary>
internal static class WinClipboard
{
    const int OpenRetries = 20, OpenRetryDelayMs = 50; // clipboard managers and RDP can hold the clipboard for a while

    // GDI handles, not memory blocks: they can't be copied byte for byte (Windows re-creates most from other formats).
    static readonly HashSet<uint> HandleFormats =
        [CF_BITMAP, CF_METAFILEPICT, CF_PALETTE, CF_ENHMETAFILE, CF_OWNERDISPLAY, CF_DSPBITMAP, CF_DSPENHMETAFILE, CF_HDROP];

    // Copied files and vector images can't be restored faithfully, so Hover won't touch a clipboard holding them.
    public static readonly uint[] Unrestorable = [CF_HDROP, CF_ENHMETAFILE, CF_METAFILEPICT];

    // Documented opt-outs: a zero DWORD under these names keeps text out of Win+V history and cloud clipboard.
    static readonly string[] PrivateFormats =
        ["ExcludeClipboardContentFromMonitorProcessing", "CanIncludeInClipboardHistory", "CanUploadToCloudClipboard"];

    /// <summary>Opens the clipboard (retrying while another app holds it); dispose to close it.</summary>
    sealed class OpenedClipboard : IDisposable
    {
        public OpenedClipboard()
        {
            for (var attempt = 1; !OpenClipboard(Owner); attempt++)
            {
                if (attempt == OpenRetries)
                    throw new IOException("the clipboard is in use by another app");
                Thread.Sleep(OpenRetryDelayMs);
            }
        }

        public void Dispose() => CloseClipboard();
    }

    /// <summary>
    /// A window of ours that owns what we put on the clipboard (Windows requires an owner after EmptyClipboard).
    /// Set once at startup.
    /// </summary>
    public static IntPtr Owner { get; set; }

    public static uint SequenceNumber => GetClipboardSequenceNumber();

    public static bool HasAny(IEnumerable<uint> formats)
    {
        using var _ = new OpenedClipboard();
        return formats.Any(IsClipboardFormatAvailable);
    }

    /// <summary>Every format we can faithfully put back, as raw bytes.</summary>
    public static Dictionary<uint, byte[]> Backup()
    {
        var saved = new Dictionary<uint, byte[]>();
        using var _ = new OpenedClipboard();
        for (var format = EnumClipboardFormats(0); format != 0; format = EnumClipboardFormats(format))
        {
            if (HandleFormats.Contains(format))
                continue;
            if (ReadBytes(GetClipboardData(format)) is { } bytes)
                saved[format] = bytes;
        }
        return saved;
    }

    public static void Restore(Dictionary<uint, byte[]> saved)
    {
        using var _ = new OpenedClipboard();
        EmptyClipboard();
        foreach (var (format, bytes) in saved)
            Put(format, bytes);
    }

    public static string Text()
    {
        using var _ = new OpenedClipboard();
        var bytes = ReadBytes(GetClipboardData(CF_UNICODETEXT));
        return bytes is null ? "" : System.Text.Encoding.Unicode.GetString(bytes).TrimEnd('\0');
    }

    /// <summary>Put text on the clipboard briefly, marked so Win+V history and cloud clipboard don't keep a copy.</summary>
    public static void SetText(string text)
    {
        using var _ = new OpenedClipboard();
        EmptyClipboard();
        Put(CF_UNICODETEXT, System.Text.Encoding.Unicode.GetBytes(text + "\0"));
        foreach (var name in PrivateFormats)
            Put(RegisterClipboardFormat(name), new byte[4]);
    }

    /// <summary>A copied image as PNG bytes: the "PNG" format Snipping Tool and browsers provide, else any bitmap.</summary>
    public static byte[]? Image()
    {
        byte[]? png;
        using (new OpenedClipboard())
            png = ReadBytes(GetClipboardData(RegisterClipboardFormat("PNG")));
        if (png is not null)
            return Images.Shrink(png);

        // Other bitmap formats: WinForms knows them all, but only on the UI (STA) thread.
        return System.Windows.Application.Current.Dispatcher.Invoke(() =>
        {
            using var bitmap = System.Windows.Forms.Clipboard.GetImage() as System.Drawing.Bitmap;
            return bitmap is null ? null : Images.Png(bitmap);
        });
    }

    static byte[]? ReadBytes(IntPtr memory)
    {
        if (memory == IntPtr.Zero)
            return null;
        var pointer = GlobalLock(memory);
        if (pointer == IntPtr.Zero)
            return null; // not a memory block (some private or delay-rendered formats)
        try
        {
            var bytes = new byte[(int)GlobalSize(memory)];
            Marshal.Copy(pointer, bytes, 0, bytes.Length);
            return bytes;
        }
        finally
        {
            GlobalUnlock(memory);
        }
    }

    static void Put(uint format, byte[] bytes)
    {
        if (bytes.Length == 0)
            return;
        var memory = GlobalAlloc(GMEM_MOVEABLE, (UIntPtr)bytes.Length);
        var pointer = GlobalLock(memory);
        if (pointer == IntPtr.Zero)
        {
            GlobalFree(memory); // out of memory: skip this format
            return;
        }
        Marshal.Copy(bytes, 0, pointer, bytes.Length);
        GlobalUnlock(memory);
        if (SetClipboardData(format, memory) == IntPtr.Zero)
            GlobalFree(memory); // the clipboard didn't take ownership: skip this format rather than lose the rest
    }
}
