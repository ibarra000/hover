using System.Windows.Input;
using System.Windows.Interop;
using static Hover.Native;

namespace Hover;

/// <summary>
/// System-wide hotkeys through Windows' RegisterHotKey. Actions run on the UI thread.
/// Combos use the same text format as before, e.g. "&lt;ctrl&gt;+&lt;alt&gt;+h" or "&lt;ctrl&gt;+&lt;shift&gt;+&lt;space&gt;".
/// </summary>
public sealed class Hotkeys : IDisposable
{
    readonly HwndSource window = new(new HwndSourceParameters("Hover hotkeys") { ParentWindow = new IntPtr(-3) }); // message-only
    readonly Dictionary<int, Action> actions = [];

    public Hotkeys() => window.AddHook(OnMessage);

    /// <summary>The hidden window that receives hotkey messages (also used as the clipboard owner).</summary>
    public IntPtr WindowHandle => window.Handle;

    /// <summary>(modifiers, virtual key) for a combo like "&lt;ctrl&gt;+&lt;alt&gt;+h"; throws FormatException if it's invalid.</summary>
    public static (uint Modifiers, uint Key) Parse(string combo)
    {
        uint modifiers = MOD_NOREPEAT; // holding the keys down doesn't repeat the action
        uint key = 0;
        foreach (var part in combo.ToLowerInvariant().Split('+'))
        {
            var name = part.Trim().Trim('<', '>');
            switch (name)
            {
                case "ctrl": modifiers |= MOD_CONTROL; break;
                case "alt": modifiers |= MOD_ALT; break;
                case "shift": modifiers |= MOD_SHIFT; break;
                case "cmd" or "win": modifiers |= MOD_WIN; break;
                default: key = KeyCode(name); break;
            }
        }
        if (key == 0)
            throw new FormatException($"\"{combo}\" has no key besides modifiers");
        return (modifiers, key);
    }

    static uint KeyCode(string name)
    {
        if (name.Length == 1 && char.IsAsciiLetterOrDigit(name[0]))
            return char.ToUpperInvariant(name[0]); // virtual-key codes for A-Z and 0-9 are their ASCII codes
        if (Enum.TryParse<Key>(name, ignoreCase: true, out var key)) // space, enter, f1, esc, ...
            return (uint)KeyInterop.VirtualKeyFromKey(key);
        throw new FormatException($"unknown key \"{name}\"");
    }

    /// <summary>Register a combo; returns an error message, or "" on success.</summary>
    public string Add(string combo, Action action)
    {
        uint modifiers, key;
        try
        {
            (modifiers, key) = Parse(combo);
        }
        catch (FormatException error)
        {
            return $"Bad hotkey in settings.json: {error.Message}";
        }
        var id = actions.Count + 1;
        if (!RegisterHotKey(window.Handle, id, modifiers, key))
            return $"{combo} is already used by another app";
        actions[id] = action;
        return "";
    }

    IntPtr OnMessage(IntPtr hwnd, int message, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        if (message == WM_HOTKEY && actions.TryGetValue(wParam.ToInt32(), out var action))
        {
            handled = true;
            action();
        }
        return IntPtr.Zero;
    }

    public void Dispose()
    {
        foreach (var id in actions.Keys)
            UnregisterHotKey(window.Handle, id);
        window.Dispose();
    }
}
