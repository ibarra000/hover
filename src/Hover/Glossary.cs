using System.IO;
using Microsoft.Data.Sqlite;

namespace Hover;

/// <summary>
/// A personal glossary of everything looked up, exportable to Anki.
/// Same sqlite file and table as the Python version, so existing glossaries carry over.
/// </summary>
public sealed class Glossary : IDisposable
{
    const int MaxTermChars = 80; // longer items are passages, not glossary terms
    const string Schema = """
        CREATE TABLE IF NOT EXISTS lookups (
            term TEXT NOT NULL, mode TEXT NOT NULL, answer TEXT NOT NULL, source TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 1, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
            PRIMARY KEY (term, mode))
        """;

    readonly SqliteConnection database;
    readonly Lock gate = new(); // lookups finish on worker threads

    public Glossary(string path)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        database = new SqliteConnection($"Data Source={path}");
        database.Open();
        Run(Schema);
    }

    /// <summary>Record a lookup; returns how often this term was looked up in this mode (0 if it isn't a term).</summary>
    public int Add(string term, string mode, string answer, string source)
    {
        term = term.Trim();
        if (term.Length == 0 || term.Length > MaxTermChars || answer.Length == 0)
            return 0;
        var now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
        lock (gate)
        {
            Run("""
                INSERT INTO lookups VALUES ($term, $mode, $answer, $source, 1, $now, $now)
                ON CONFLICT (term, mode) DO UPDATE SET answer = excluded.answer, source = excluded.source,
                    count = count + 1, last_seen = excluded.last_seen
                """, ("$term", term), ("$mode", mode), ("$answer", answer), ("$source", source), ("$now", now));
            using var query = Command("SELECT count FROM lookups WHERE term = $term AND mode = $mode",
                                      ("$term", term), ("$mode", mode));
            return Convert.ToInt32(query.ExecuteScalar());
        }
    }

    public List<(string Term, string Mode, string Answer, string Source, int Count)> Rows()
    {
        var rows = new List<(string, string, string, string, int)>();
        lock (gate)
        {
            using var query = Command("SELECT term, mode, answer, source, count FROM lookups ORDER BY last_seen DESC");
            using var reader = query.ExecuteReader();
            while (reader.Read())
                rows.Add((reader.GetString(0), reader.GetString(1), reader.GetString(2), reader.GetString(3), reader.GetInt32(4)));
        }
        return rows;
    }

    /// <summary>Write an Anki-importable CSV (front = term, back = answer + source). Returns the number of cards.</summary>
    public int ExportAnki(string path)
    {
        var rows = Rows();
        using var file = new StreamWriter(path, append: false, new System.Text.UTF8Encoding(false));
        file.Write("#separator:Comma\n#html:false\n#columns:Front,Back,Tags\n");
        foreach (var row in rows)
        {
            var back = $"{row.Answer}\n\n{row.Source}".Trim();
            file.Write($"{CsvCell(Inert(row.Term))},{CsvCell(Inert(back))},{CsvCell($"hover {row.Mode}")}\n");
        }
        return rows.Count;
    }

    /// <summary>Stop spreadsheet apps treating a copied "=cmd|..." term as a formula if the CSV is opened in Excel.</summary>
    public static string Inert(string cell) => cell.Length > 0 && "=+-@\t\r".Contains(cell[0]) ? $"'{cell}" : cell;

    static string CsvCell(string cell)
    {
        var needsQuotes = cell.IndexOfAny([',', '"', '\n', '\r']) >= 0;
        return needsQuotes ? $"\"{cell.Replace("\"", "\"\"")}\"" : cell;
    }

    void Run(string sql, params (string Name, object Value)[] parameters)
    {
        using var command = Command(sql, parameters);
        command.ExecuteNonQuery();
    }

    SqliteCommand Command(string sql, params (string Name, object Value)[] parameters)
    {
        var command = database.CreateCommand();
        command.CommandText = sql;
        foreach (var (name, value) in parameters)
            command.Parameters.AddWithValue(name, value);
        return command;
    }

    public void Dispose() => database.Dispose();
}
