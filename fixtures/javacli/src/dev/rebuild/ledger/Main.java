package dev.rebuild.ledger;

import java.io.IOException;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.List;

/**
 * javacli: tiny ledger CLI.
 * Exit codes: 0 ok, 1 usage / unknown command / bad name, 2 corrupt or unwritable ledger, 3 entry not found, 4 bad amount.
 */
public final class Main {
    private Main() {
    }

    public static void main(String[] args) {
        PrintStream out = new PrintStream(System.out, true, StandardCharsets.UTF_8);
        PrintStream err = new PrintStream(System.err, true, StandardCharsets.UTF_8);
        System.exit(run(args, out, err));
    }

    static int run(String[] args, PrintStream out, PrintStream err) {
        if (args.length < 2) {
            err.print("usage: javacli <ledger.txt> add <name> <amount> | remove <name> | list | total | stats\n");
            return 1;
        }
        Path file = Path.of(args[0]);
        String cmd = args[1];
        Ledger ledger;
        try {
            ledger = Ledger.load(file);
        } catch (Ledger.CorruptException e) {
            err.print("ledger is corrupt (" + e.getMessage() + "); refusing to touch " + args[0] + "\n");
            return 2;
        } catch (IOException e) {
            err.print("cannot read " + args[0] + "\n");
            return 2;
        }
        try {
            switch (cmd) {
                case "add": {
                    if (args.length != 4) {
                        err.print("usage: javacli <ledger.txt> add <name> <amount>\n");
                        return 1;
                    }
                    long cents = Money.parse(args[3]);
                    boolean replaced = ledger.put(args[2], cents);
                    ledger.save(file);
                    out.print((replaced ? "updated " : "added ") + args[2] + " " + Money.format(cents) + "\n");
                    return 0;
                }
                case "remove": {
                    if (args.length != 3) {
                        err.print("usage: javacli <ledger.txt> remove <name>\n");
                        return 1;
                    }
                    if (!ledger.remove(args[2])) {
                        err.print("no such entry: " + args[2] + "\n");
                        return 3;
                    }
                    ledger.save(file);
                    out.print("removed " + args[2] + "\n");
                    return 0;
                }
                case "list": {
                    List<Entry> rows = ledger.sorted();
                    if (rows.isEmpty()) {
                        out.print("(empty)\n");
                    }
                    for (Entry e : rows) {
                        out.print(pad(e.name(), 12) + " " + padLeft(e.formatted(), 10) + "\n");
                    }
                    return 0;
                }
                case "total":
                    out.print("total " + Money.format(ledger.total()) + " (" + ledger.size() + " entries)\n");
                    return 0;
                case "stats": {
                    List<Entry> rows = ledger.sorted();
                    if (rows.isEmpty()) {
                        out.print("no data\n");
                        return 0;
                    }
                    Entry min = rows.get(0);
                    Entry max = rows.get(0);
                    for (Entry e : rows) {
                        if (e.cents() < min.cents()) {
                            min = e;
                        }
                        if (e.cents() > max.cents()) {
                            max = e;
                        }
                    }
                    long mean = Math.floorDiv(ledger.total(), rows.size());
                    out.print("min " + min.name() + " " + min.formatted() + "\n");
                    out.print("max " + max.name() + " " + max.formatted() + "\n");
                    out.print("mean " + Money.format(mean) + "\n");
                    return 0;
                }
                default:
                    err.print("unknown command: " + cmd + "\n");
                    return 1;
            }
        } catch (NumberFormatException | ArithmeticException e) {
            err.print("bad amount: " + e.getMessage() + "\n");
            return 4;
        } catch (IllegalArgumentException e) {
            err.print("bad name\n");
            return 1;
        } catch (IOException e) {
            err.print("cannot write " + args[0] + "\n");
            return 2;
        }
    }

    private static String pad(String s, int width) {
        StringBuilder sb = new StringBuilder(s);
        while (sb.length() < width) {
            sb.append(' ');
        }
        return sb.toString();
    }

    private static String padLeft(String s, int width) {
        StringBuilder sb = new StringBuilder();
        for (int i = s.length(); i < width; i++) {
            sb.append(' ');
        }
        return sb.append(s).toString();
    }
}
