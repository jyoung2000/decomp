// benchnet: task-queue CLI used as R0 benchmark ground truth (Rebuild Studio fixtures/bench). Written for this repository.
// usage: benchnet <plan|order|stats> task:priority[:dep,dep] ...
// Exit codes: 0 ok, 1 usage, 2 parse error, 3 dependency cycle.
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;

namespace BenchNet
{
    public enum Priority { Low = 1, Normal = 2, High = 3, Critical = 4 }

    public interface ITaskSource
    {
        IReadOnlyList<WorkItem> Load();
    }

    public sealed class WorkItem
    {
        public string Name { get; }
        public Priority Priority { get; }
        public List<string> DependsOn { get; } = new List<string>();

        public WorkItem(string name, Priority priority)
        {
            Name = name;
            Priority = priority;
        }

        public override string ToString() => $"{Name}({Priority})";
    }

    public sealed class ParseException : Exception
    {
        public string Spec { get; }
        public ParseException(string spec, string message) : base(message) { Spec = spec; }
    }

    public sealed class ArgumentTaskSource : ITaskSource
    {
        private readonly string[] _specs;
        public ArgumentTaskSource(string[] specs) { _specs = specs; }

        public IReadOnlyList<WorkItem> Load() => _specs.Select(ParseSpec).ToList();

        internal static WorkItem ParseSpec(string spec)
        {
            var parts = spec.Split(':');
            if (parts.Length < 2 || parts[0].Length == 0)
                throw new ParseException(spec, "expected task:priority[:dep,dep]");
            if (!Enum.TryParse(parts[1], true, out Priority p) || !Enum.IsDefined(typeof(Priority), p))
                throw new ParseException(spec, $"unknown priority '{parts[1]}'");
            var item = new WorkItem(parts[0], p);
            if (parts.Length > 2)
                item.DependsOn.AddRange(parts[2].Split(',', StringSplitOptions.RemoveEmptyEntries));
            return item;
        }
    }

    public sealed class Scheduler
    {
        public sealed class CycleException : Exception
        {
            public CycleException(string at) : base($"dependency cycle at '{at}'") { }
        }

        private readonly Dictionary<string, WorkItem> _byName;

        public Scheduler(IEnumerable<WorkItem> items)
        {
            _byName = new Dictionary<string, WorkItem>(StringComparer.Ordinal);
            foreach (var it in items) _byName[it.Name] = it;
        }

        public List<WorkItem> TopologicalOrder()
        {
            var order = new List<WorkItem>();
            var state = new Dictionary<string, int>();
            foreach (var name in _byName.Keys.OrderBy(n => n, StringComparer.Ordinal))
                Visit(name, state, order);
            return order;
        }

        private void Visit(string name, Dictionary<string, int> state, List<WorkItem> order)
        {
            state.TryGetValue(name, out int s);
            if (s == 2) return;
            if (s == 1) throw new CycleException(name);
            state[name] = 1;
            if (_byName.TryGetValue(name, out var item))
            {
                foreach (var dep in item.DependsOn) Visit(dep, state, order);
                order.Add(item);
            }
            state[name] = 2;
        }

        public List<WorkItem> PriorityPlan() =>
            _byName.Values.OrderByDescending(i => i.Priority).ThenBy(i => i.Name, StringComparer.Ordinal).ToList();
    }

    public static class Report
    {
        public static string Stats(IReadOnlyList<WorkItem> items)
        {
            var sb = new StringBuilder();
            sb.AppendLine($"tasks={items.Count} deps={items.Sum(i => i.DependsOn.Count)}");
            foreach (var g in items.GroupBy(i => i.Priority).OrderByDescending(g => g.Key))
                sb.AppendLine($"  {g.Key,-8} {g.Count()}");
            return sb.ToString();
        }

        public static string Join(IEnumerable<WorkItem> items) => string.Join(" -> ", items.Select(i => i.ToString()));
    }

    public static class Program
    {
        private const string Usage = "benchnet 1.0 - Rebuild Studio benchmark fixture\nusage: benchnet <plan|order|stats> task:priority[:dep,dep] ...";

        public static int Main(string[] args)
        {
            if (args.Length < 2) { Console.Error.WriteLine(Usage); return 1; }
            IReadOnlyList<WorkItem> items;
            try { items = new ArgumentTaskSource(args.Skip(1).ToArray()).Load(); }
            catch (ParseException e) { Console.Error.WriteLine($"benchnet: bad spec '{e.Spec}': {e.Message}"); return 2; }
            var sched = new Scheduler(items);
            switch (args[0])
            {
                case "plan": Console.WriteLine(Report.Join(sched.PriorityPlan())); return 0;
                case "order":
                    try { Console.WriteLine(Report.Join(sched.TopologicalOrder())); return 0; }
                    catch (Scheduler.CycleException e) { Console.Error.WriteLine("benchnet: " + e.Message); return 3; }
                case "stats": Console.Write(Report.Stats(items)); return 0;
                default: Console.Error.WriteLine(Usage); return 1;
            }
        }
    }
}
