using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;

namespace NotesApp
{
    public sealed class AppState
    {
        public int Version { get; set; } = 1;
        public string UserName { get; set; } = "guest";
        public string Theme { get; set; } = "light";
        public List<string> Notes { get; set; } = new List<string>();

        private static readonly JsonSerializerOptions Options = new JsonSerializerOptions
        {
            WriteIndented = true,
        };

        /// <summary>Loads state; a missing file yields defaults, malformed JSON throws StateCorruptException.</summary>
        public static AppState Load(string path)
        {
            if (!File.Exists(path))
            {
                return new AppState();
            }
            try
            {
                string text = File.ReadAllText(path);
                AppState? s = JsonSerializer.Deserialize<AppState>(text, Options);
                if (s == null || s.Notes == null || s.Theme == null || s.UserName == null)
                {
                    throw new StateCorruptException("state file has missing fields");
                }
                return s;
            }
            catch (JsonException)
            {
                throw new StateCorruptException("state file is not valid JSON");
            }
        }

        public void Save(string path)
        {
            File.WriteAllText(path, JsonSerializer.Serialize(this, Options) + "\n");
        }
    }

    public sealed class StateCorruptException : Exception
    {
        public StateCorruptException(string message) : base(message) { }
    }
}
