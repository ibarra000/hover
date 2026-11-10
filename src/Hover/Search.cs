using System.Net;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Hover;

public sealed record SearchResult(string Title, string Url, string Snippet);

/// <summary>Web search used to ground answers (swapped for a fake in tests).</summary>
public delegate Task<IReadOnlyList<SearchResult>> WebSearch(string query, int count, CancellationToken cancel);

/// <summary>Results from DuckDuckGo's plain HTML page (no API key, no browser needed).</summary>
public static class DuckDuckGo
{
    const int MaxQueryChars = 200; // only this much of the copied text is ever sent to the search engine
    const string SearchUrl = "https://html.duckduckgo.com/html/?q=";

    static readonly HttpClient Http = CreateClient();

    // Each result is a title link (class="result__a") followed by a snippet link (class="result__snippet").
    static readonly Regex ResultPattern = new(
        """class="result__a"[^>]*href="(?<href>[^"]*)"[^>]*>(?<title>.*?)</a>.*?class="result__snippet"[^>]*>(?<snippet>.*?)</a>""",
        RegexOptions.Singleline);
    static readonly Regex Tags = new("<[^>]+>");

    static HttpClient CreateClient()
    {
        var client = new HttpClient { Timeout = TimeSpan.FromSeconds(8) };
        client.DefaultRequestHeaders.UserAgent.ParseAdd("Mozilla/5.0 (Windows NT 10.0; Win64; x64)");
        return client;
    }

    public static async Task<IReadOnlyList<SearchResult>> Search(string query, int count, CancellationToken cancel)
    {
        try
        {
            var html = await Http.GetStringAsync(SearchUrl + Uri.EscapeDataString(Shorten(query)), cancel);
            return Parse(html).Take(count).ToList();
        }
        catch (Exception error) when ((error is HttpRequestException or TaskCanceledException) && !cancel.IsCancellationRequested)
        {
            return []; // offline or blocked: the card marks the answer as local
        }
    }

    /// <summary>Only the first MaxQueryChars of the copied text are ever sent to a search engine.</summary>
    public static string Shorten(string query) => query.Length > MaxQueryChars ? query[..MaxQueryChars] : query;

    public static IEnumerable<SearchResult> Parse(string html)
    {
        foreach (Match match in ResultPattern.Matches(html))
        {
            yield return new SearchResult(
                Title: PlainText(match.Groups["title"].Value),
                Url: RealUrl(WebUtility.HtmlDecode(match.Groups["href"].Value)),
                Snippet: PlainText(match.Groups["snippet"].Value));
        }
    }

    public static string PlainText(string html) => WebUtility.HtmlDecode(Tags.Replace(html, "")).Trim();

    /// <summary>DuckDuckGo wraps each link as //duckduckgo.com/l/?uddg=&lt;the real URL&gt;&amp;...</summary>
    static string RealUrl(string href)
    {
        var marker = href.IndexOf("uddg=", StringComparison.Ordinal);
        if (marker < 0)
            return href;
        var encoded = href[(marker + "uddg=".Length)..].Split('&')[0];
        return Uri.UnescapeDataString(encoded);
    }
}

/// <summary>Wikipedia's official search API: stable and free, used when DuckDuckGo returns nothing.</summary>
public static class Wikipedia
{
    static readonly HttpClient Http = CreateClient();

    static HttpClient CreateClient()
    {
        var client = new HttpClient { Timeout = TimeSpan.FromSeconds(8) };
        client.DefaultRequestHeaders.UserAgent.ParseAdd("Hover/2.0 (https://github.com/ibarra000/clipctx)"); // Wikimedia asks for this
        return client;
    }

    public static async Task<IReadOnlyList<SearchResult>> Search(string query, int count, CancellationToken cancel)
    {
        try
        {
            var terms = Uri.EscapeDataString(DuckDuckGo.Shorten(query));
            var url = $"https://en.wikipedia.org/w/rest.php/v1/search/page?limit={count}&q={terms}";
            var response = await Http.GetFromJsonAsync<SearchResponse>(url, cancel);
            var results = new List<SearchResult>();
            foreach (var page in response?.Pages ?? [])
            {
                var pageUrl = $"https://en.wikipedia.org/wiki/{Uri.EscapeDataString(page.Key)}";
                results.Add(new SearchResult(page.Title, pageUrl, DuckDuckGo.PlainText(page.Excerpt ?? "")));
            }
            return results;
        }
        catch (Exception error) when ((error is HttpRequestException or TaskCanceledException or JsonException)
                                      && !cancel.IsCancellationRequested)
        {
            return [];
        }
    }

    sealed record SearchResponse(List<Page>? Pages);
    sealed record Page(string Key, string Title, string? Excerpt);
}

public static class WebSearches
{
    /// <summary>DuckDuckGo first (best for code and errors); Wikipedia if it has nothing or is rate limiting us.</summary>
    public static async Task<IReadOnlyList<SearchResult>> DuckDuckGoThenWikipedia(string query, int count,
                                                                                   CancellationToken cancel)
    {
        var results = await DuckDuckGo.Search(query, count, cancel);
        return results.Count > 0 ? results : await Wikipedia.Search(query, count, cancel);
    }
}

public static class SafeLinks
{
    /// <summary>The first plain http(s) URL with a real host ("" if none): only these are ever opened from the card.</summary>
    public static string First(IEnumerable<SearchResult> results)
    {
        foreach (var result in results)
        {
            if (IsSafe(result.Url))
                return result.Url;
        }
        return "";
    }

    public static bool IsSafe(string url)
    {
        if (url.Any(c => char.IsWhiteSpace(c) || char.IsControl(c) || c == '"'))
            return false; // could break out of the shell command that opens the browser
        return Uri.TryCreate(url, UriKind.Absolute, out var uri)
               && (uri.Scheme == Uri.UriSchemeHttp || uri.Scheme == Uri.UriSchemeHttps)
               && uri.Host.Length > 0;
    }
}
