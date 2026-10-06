package dev.rebuild.ledger;

/** Cent-exact parsing and formatting; no floating point is used for stored values. */
public final class Money {
    private Money() {
    }

    public static long parse(String text) {
        String t = text.trim();
        boolean negative = t.startsWith("-");
        if (negative) {
            t = t.substring(1);
        }
        int dot = t.indexOf('.');
        String whole = dot < 0 ? t : t.substring(0, dot);
        String frac = dot < 0 ? "00" : t.substring(dot + 1);
        if (whole.isEmpty() || frac.length() == 0 || frac.length() > 2 || !digits(whole) || !digits(frac)) {
            throw new NumberFormatException("not an amount: " + text);
        }
        if (frac.length() == 1) {
            frac = frac + "0";
        }
        long cents = Math.addExact(Math.multiplyExact(Long.parseLong(whole), 100L), Long.parseLong(frac));
        return negative ? -cents : cents;
    }

    public static String format(long cents) {
        long abs = Math.abs(cents);
        return (cents < 0 ? "-" : "") + (abs / 100) + "." + (abs % 100 < 10 ? "0" : "") + (abs % 100);
    }

    private static boolean digits(String s) {
        for (int i = 0; i < s.length(); i++) {
            if (s.charAt(i) < '0' || s.charAt(i) > '9') {
                return false;
            }
        }
        return true;
    }
}
