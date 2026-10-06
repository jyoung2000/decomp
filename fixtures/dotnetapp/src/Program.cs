using System;
using System.IO;
using System.Text;

namespace NotesApp
{
    public static class Program
    {
        // Exit codes: 0 ok, 2 usage, 4 corrupt state file.
        public static int Main(string[] args)
        {
            if (args.Length != 1)
            {
                Console.Error.WriteLine("usage: dotnetapp <state.json>");
                return 2;
            }
            string path = args[0];
            AppState state;
            try
            {
                state = AppState.Load(path);
            }
            catch (StateCorruptException ex)
            {
                Console.Error.WriteLine("Sorry, your saved data in '" + path + "' could not be read (" + ex.Message + ").");
                Console.Error.WriteLine("Please fix or delete the file and start again.");
                return 4;
            }
            var stdout = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { NewLine = "\n", AutoFlush = true };
            var stdin = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false));
            stdout.WriteLine("Notes App 1.0 - hello, " + state.UserName + " (" + state.Notes.Count + " notes, theme " + state.Theme + ")");
            new Menus(state, path, stdin, stdout).RunMain();
            state.Save(path);
            stdout.WriteLine("Goodbye, " + state.UserName + ".");
            return 0;
        }
    }
}
