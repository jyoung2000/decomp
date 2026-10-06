using System;
using System.IO;

namespace NotesApp
{
    /// <summary>Stdin-driven menu tree: main -> notes / settings -> back.</summary>
    public sealed class Menus
    {
        private readonly AppState _state;
        private readonly string _path;
        private readonly TextReader _in;
        private readonly TextWriter _out;

        public Menus(AppState state, string path, TextReader input, TextWriter output)
        {
            _state = state;
            _path = path;
            _in = input;
            _out = output;
        }

        private string? Prompt(string text)
        {
            _out.Write(text);
            _out.Flush();
            string? line = _in.ReadLine();
            if (line == null)
            {
                _out.WriteLine();
                return null;
            }
            return line.Trim();
        }

        public void RunMain()
        {
            while (true)
            {
                _out.WriteLine("=== Main Menu ===");
                _out.WriteLine("1) Notes");
                _out.WriteLine("2) Settings");
                _out.WriteLine("0) Quit");
                string? c = Prompt("> ");
                if (c == null || c == "0" || c == "q")
                {
                    return;
                }
                if (c == "1") { RunNotes(); }
                else if (c == "2") { RunSettings(); }
                else { _out.WriteLine("Unknown choice: " + c); }
            }
        }

        private void RunNotes()
        {
            while (true)
            {
                _out.WriteLine("--- Notes (" + _state.Notes.Count + ") ---");
                _out.WriteLine("1) Add note");
                _out.WriteLine("2) List notes");
                _out.WriteLine("3) Delete note");
                _out.WriteLine("0) Back");
                string? c = Prompt("notes> ");
                if (c == null || c == "0") { return; }
                if (c == "1")
                {
                    string? text = Prompt("Text: ");
                    if (string.IsNullOrEmpty(text))
                    {
                        _out.WriteLine("Note not added (empty).");
                        continue;
                    }
                    _state.Notes.Add(text);
                    _state.Save(_path);
                    _out.WriteLine("Added note #" + _state.Notes.Count + ".");
                }
                else if (c == "2")
                {
                    if (_state.Notes.Count == 0) { _out.WriteLine("(no notes)"); }
                    for (int i = 0; i < _state.Notes.Count; i++)
                    {
                        _out.WriteLine((i + 1) + ". " + _state.Notes[i]);
                    }
                }
                else if (c == "3")
                {
                    string? n = Prompt("Delete which number? ");
                    if (int.TryParse(n, out int idx) && idx >= 1 && idx <= _state.Notes.Count)
                    {
                        string removed = _state.Notes[idx - 1];
                        _state.Notes.RemoveAt(idx - 1);
                        _state.Save(_path);
                        _out.WriteLine("Deleted: " + removed);
                    }
                    else
                    {
                        _out.WriteLine("No such note.");
                    }
                }
                else { _out.WriteLine("Unknown choice: " + c); }
            }
        }

        private void RunSettings()
        {
            while (true)
            {
                _out.WriteLine("--- Settings ---");
                _out.WriteLine("1) User name: " + _state.UserName);
                _out.WriteLine("2) Theme: " + _state.Theme);
                _out.WriteLine("0) Back");
                string? c = Prompt("settings> ");
                if (c == null || c == "0") { return; }
                if (c == "1")
                {
                    string? name = Prompt("New name: ");
                    if (string.IsNullOrEmpty(name)) { _out.WriteLine("Name unchanged."); continue; }
                    _state.UserName = name;
                    _state.Save(_path);
                    _out.WriteLine("Name set to " + name + ".");
                }
                else if (c == "2")
                {
                    _state.Theme = _state.Theme == "light" ? "dark" : "light";
                    _state.Save(_path);
                    _out.WriteLine("Theme is now " + _state.Theme + ".");
                }
                else { _out.WriteLine("Unknown choice: " + c); }
            }
        }
    }
}
