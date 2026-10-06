package dev.rebuild.ledger;

/** One ledger line: a unique name and an amount in cents. */
public record Entry(String name, long cents) {
    public Entry {
        if (name == null || name.isEmpty() || name.indexOf('\t') >= 0 || name.indexOf('\n') >= 0) {
            throw new IllegalArgumentException("bad name");
        }
    }

    public String formatted() {
        return Money.format(cents);
    }
}
