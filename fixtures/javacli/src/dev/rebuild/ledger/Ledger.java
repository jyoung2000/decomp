package dev.rebuild.ledger;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/** Ledger persisted as a text file: header line "LEDGER1" then "name<TAB>cents" lines. */
public final class Ledger {
    public static final String HEADER = "LEDGER1";

    public static final class CorruptException extends Exception {
        public CorruptException(String message) {
            super(message);
        }
    }

    private final List<Entry> entries = new ArrayList<>();

    public static Ledger load(Path path) throws IOException, CorruptException {
        Ledger ledger = new Ledger();
        if (!Files.exists(path)) {
            return ledger;
        }
        List<String> lines = Files.readAllLines(path, StandardCharsets.UTF_8);
        if (lines.isEmpty() || !lines.get(0).equals(HEADER)) {
            throw new CorruptException("missing header");
        }
        for (int i = 1; i < lines.size(); i++) {
            String[] parts = lines.get(i).split("\t", -1);
            if (parts.length != 2) {
                throw new CorruptException("line " + (i + 1) + ": expected 2 fields");
            }
            try {
                ledger.entries.add(new Entry(parts[0], Long.parseLong(parts[1])));
            } catch (IllegalArgumentException e) {
                throw new CorruptException("line " + (i + 1) + ": " + e.getMessage());
            }
        }
        return ledger;
    }

    public void save(Path path) throws IOException {
        StringBuilder sb = new StringBuilder(HEADER).append('\n');
        for (Entry e : entries) {
            sb.append(e.name()).append('\t').append(e.cents()).append('\n');
        }
        Files.write(path, sb.toString().getBytes(StandardCharsets.UTF_8));
    }

    /** Adds a new entry or replaces the amount of an existing one; returns true when replaced. */
    public boolean put(String name, long cents) {
        for (int i = 0; i < entries.size(); i++) {
            if (entries.get(i).name().equals(name)) {
                entries.set(i, new Entry(name, cents));
                return true;
            }
        }
        entries.add(new Entry(name, cents));
        return false;
    }

    public boolean remove(String name) {
        return entries.removeIf(e -> e.name().equals(name));
    }

    public List<Entry> sorted() {
        List<Entry> copy = new ArrayList<>(entries);
        copy.sort(Comparator.comparing(Entry::name));
        return copy;
    }

    public long total() {
        return entries.stream().mapToLong(Entry::cents).sum();
    }

    public int size() {
        return entries.size();
    }
}
