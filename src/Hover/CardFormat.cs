using System.Text.RegularExpressions;

namespace Hover;

public enum TextStyle { Plain, Bold, Code }

/// <summary>The card's pure helpers: light markdown, placement near the cursor, and the footer text.</summary>
public static class CardFormat
{
    public const int CursorOffset = 16;

    static readonly Regex Markdown = new(@"```[^\n]*\n?(?<block>.*?)```|`(?<code>[^`\n]+)`|\*\*(?<bold>.+?)\*\*",
                                         RegexOptions.Singleline);
    static readonly Regex Bullet = new(@"^(\s*)[-*] ", RegexOptions.Multiline);

    /// <summary>Light markdown (```blocks```, `code`, **bold**, - bullets) as styled runs of text.</summary>
    public static List<(string Text, TextStyle Style)> Segments(string text)
    {
        text = Bullet.Replace(text, "$1• ");
        var runs = new List<(string, TextStyle)>();
        var position = 0;
        foreach (Match match in Markdown.Matches(text))
        {
            runs.Add((text[position..match.Index], TextStyle.Plain));
            if (match.Groups["bold"].Success)
                runs.Add((match.Groups["bold"].Value, TextStyle.Bold));
            else if (match.Groups["block"].Success)
                runs.Add((match.Groups["block"].Value, TextStyle.Code));
            else
                runs.Add((match.Groups["code"].Value, TextStyle.Code));
            position = match.Index + match.Length;
        }
        runs.Add((text[position..], TextStyle.Plain));
        return runs.Where(run => run.Item1.Length > 0).ToList();
    }

    /// <summary>Top-left for a card near (x, y) inside a monitor's work area, flipping to the other side of the cursor
    /// at the edges. All values in physical pixels.</summary>
    public static (int X, int Y) Fit(int x, int y, int width, int height, System.Drawing.Rectangle area)
    {
        if (x + width > area.Right)
            x -= width + 2 * CursorOffset;
        if (y + height > area.Bottom)
            y -= height + 2 * CursorOffset;
        // Keep it on screen; if it's bigger than the screen, pin its top-left corner.
        return (Math.Max(area.Left, Math.Min(x, area.Right - width)), Math.Max(area.Top, Math.Min(y, area.Bottom - height)));
    }

    /// <summary>Privacy badge first: "web · host ↗" when a search was used, "local" when nothing left the PC.</summary>
    public static string Footer(string link, string mode, int turns, int timesLookedUp)
    {
        var parts = new List<string> { link.Length > 0 ? $"web · {new Uri(link).Host} ↗" : "local", mode };
        if (turns > 1)
            parts.Add($"{turns} in context");
        if (timesLookedUp > 1)
            parts.Add($"looked up {timesLookedUp}×");
        return string.Join(" · ", parts);
    }
}
