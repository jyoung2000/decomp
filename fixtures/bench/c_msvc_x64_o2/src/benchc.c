/* benchc: small text-tools CLI used as R0 benchmark ground truth (Rebuild Studio fixtures/bench).
 * Commands: wc <file> | crc <file> | rle <file> | b64 <file> | hist <file> | sort <file> | rev <file> | stats <n...>
 * Exit codes: 0 ok, 1 usage, 2 cannot open, 3 too large. Output on stdout, errors on stderr.
 * Written for this repository; public domain for the purpose of the benchmark. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

#define BENCH_MAX_INPUT (1u << 20)
#define BENCH_MAX_LINES 4096

#if defined(_MSC_VER)
#define NOINLINE __declspec(noinline)
#else
#define NOINLINE __attribute__((noinline))
#endif

typedef struct {
    unsigned char *data;
    size_t len;
} Buffer;

typedef struct {
    unsigned long lines, words, bytes, max_line;
} Counts;

static const char *BENCH_BANNER = "benchc 1.0 - Rebuild Studio benchmark fixture";
static const char *BENCH_USAGE =
    "usage: benchc <wc|crc|rle|b64|hist|sort|rev> <file>\n"
    "       benchc stats <n> [n...]\n";
static const char B64_ALPHABET[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
static unsigned long crc_table[256];
static int crc_ready = 0;

NOINLINE static void crc_init(void) {
    for (unsigned long n = 0; n < 256; n++) {
        unsigned long c = n;
        for (int k = 0; k < 8; k++)
            c = (c & 1) ? 0xEDB88320UL ^ (c >> 1) : c >> 1;
        crc_table[n] = c;
    }
    crc_ready = 1;
}

NOINLINE unsigned long bench_crc32(const unsigned char *p, size_t n) {
    unsigned long c = 0xFFFFFFFFUL;
    if (!crc_ready)
        crc_init();
    for (size_t i = 0; i < n; i++)
        c = crc_table[(c ^ p[i]) & 0xFF] ^ (c >> 8);
    return c ^ 0xFFFFFFFFUL;
}

NOINLINE static int read_file(const char *path, Buffer *out) {
    FILE *f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "benchc: cannot open '%s'\n", path);
        return 2;
    }
    out->data = (unsigned char *)malloc(BENCH_MAX_INPUT + 1);
    if (!out->data) {
        fclose(f);
        return 3;
    }
    out->len = fread(out->data, 1, BENCH_MAX_INPUT + 1, f);
    fclose(f);
    if (out->len > BENCH_MAX_INPUT) {
        fprintf(stderr, "benchc: input larger than %u bytes\n", BENCH_MAX_INPUT);
        free(out->data);
        return 3;
    }
    out->data[out->len] = 0;
    return 0;
}

NOINLINE Counts bench_count(const Buffer *b) {
    Counts c = {0, 0, 0, 0};
    int in_word = 0;
    unsigned long line_len = 0;
    for (size_t i = 0; i < b->len; i++) {
        unsigned char ch = b->data[i];
        c.bytes++;
        if (ch == '\n') {
            c.lines++;
            if (line_len > c.max_line)
                c.max_line = line_len;
            line_len = 0;
        } else {
            line_len++;
        }
        if (isspace(ch)) {
            in_word = 0;
        } else if (!in_word) {
            in_word = 1;
            c.words++;
        }
    }
    if (line_len > c.max_line)
        c.max_line = line_len;
    return c;
}

NOINLINE static void cmd_wc(const Buffer *b) {
    Counts c = bench_count(b);
    printf("lines=%lu words=%lu bytes=%lu longest=%lu\n", c.lines, c.words, c.bytes, c.max_line);
}

NOINLINE static void cmd_crc(const Buffer *b) {
    printf("crc32=%08lx size=%lu\n", bench_crc32(b->data, b->len), (unsigned long)b->len);
}

NOINLINE size_t bench_rle_encode(const unsigned char *in, size_t n, unsigned char *out) {
    size_t o = 0;
    for (size_t i = 0; i < n;) {
        size_t run = 1;
        while (i + run < n && in[i + run] == in[i] && run < 255)
            run++;
        out[o++] = (unsigned char)run;
        out[o++] = in[i];
        i += run;
    }
    return o;
}

NOINLINE static void cmd_rle(const Buffer *b) {
    unsigned char *out = (unsigned char *)malloc(b->len * 2 + 2);
    size_t n;
    if (!out)
        return;
    n = bench_rle_encode(b->data, b->len, out);
    printf("rle: %lu -> %lu bytes, ratio %.3f\n", (unsigned long)b->len, (unsigned long)n,
           b->len ? (double)n / (double)b->len : 0.0);
    free(out);
}

NOINLINE size_t bench_base64(const unsigned char *in, size_t n, char *out) {
    size_t o = 0, i = 0;
    for (; i + 2 < n; i += 3) {
        unsigned long v = ((unsigned long)in[i] << 16) | ((unsigned long)in[i + 1] << 8) | in[i + 2];
        out[o++] = B64_ALPHABET[(v >> 18) & 63];
        out[o++] = B64_ALPHABET[(v >> 12) & 63];
        out[o++] = B64_ALPHABET[(v >> 6) & 63];
        out[o++] = B64_ALPHABET[v & 63];
    }
    if (i < n) {
        unsigned long v = (unsigned long)in[i] << 16;
        if (i + 1 < n)
            v |= (unsigned long)in[i + 1] << 8;
        out[o++] = B64_ALPHABET[(v >> 18) & 63];
        out[o++] = B64_ALPHABET[(v >> 12) & 63];
        out[o++] = (i + 1 < n) ? B64_ALPHABET[(v >> 6) & 63] : '=';
        out[o++] = '=';
    }
    out[o] = 0;
    return o;
}

NOINLINE static void cmd_b64(const Buffer *b) {
    char *out = (char *)malloc(b->len / 3 * 4 + 8);
    if (!out)
        return;
    bench_base64(b->data, b->len, out);
    puts(out);
    free(out);
}

NOINLINE static void cmd_hist(const Buffer *b) {
    unsigned long hist[26] = {0};
    unsigned long total = 0;
    for (size_t i = 0; i < b->len; i++) {
        int ch = tolower(b->data[i]);
        if (ch >= 'a' && ch <= 'z') {
            hist[ch - 'a']++;
            total++;
        }
    }
    for (int k = 0; k < 26; k++) {
        if (hist[k])
            printf("%c %6lu %5.1f%%\n", 'a' + k, hist[k], total ? 100.0 * hist[k] / total : 0.0);
    }
}

NOINLINE static int compare_lines(const void *a, const void *b) {
    return strcmp(*(const char *const *)a, *(const char *const *)b);
}

NOINLINE static size_t split_lines(Buffer *b, char **lines, size_t max) {
    size_t n = 0;
    char *p = (char *)b->data;
    while (*p && n < max) {
        char *nl = strchr(p, '\n');
        lines[n++] = p;
        if (!nl)
            break;
        *nl = 0;
        if (nl > p && nl[-1] == '\r')
            nl[-1] = 0;
        p = nl + 1;
    }
    return n;
}

NOINLINE static void cmd_sort(Buffer *b) {
    char **lines = (char **)malloc(sizeof(char *) * BENCH_MAX_LINES);
    size_t n;
    if (!lines)
        return;
    n = split_lines(b, lines, BENCH_MAX_LINES);
    qsort(lines, n, sizeof(char *), compare_lines);
    for (size_t i = 0; i < n; i++)
        puts(lines[i]);
    free(lines);
}

NOINLINE static void reverse_in_place(char *s) {
    size_t n = strlen(s);
    for (size_t i = 0; i < n / 2; i++) {
        char t = s[i];
        s[i] = s[n - 1 - i];
        s[n - 1 - i] = t;
    }
}

NOINLINE static void cmd_rev(Buffer *b) {
    char **lines = (char **)malloc(sizeof(char *) * BENCH_MAX_LINES);
    size_t n;
    if (!lines)
        return;
    n = split_lines(b, lines, BENCH_MAX_LINES);
    for (size_t i = 0; i < n; i++) {
        reverse_in_place(lines[i]);
        puts(lines[i]);
    }
    free(lines);
}

NOINLINE static int cmd_stats(int argc, char **argv) {
    long min = 0, max = 0, sum = 0;
    for (int i = 0; i < argc; i++) {
        char *end = NULL;
        long v = strtol(argv[i], &end, 10);
        if (!end || *end) {
            fprintf(stderr, "benchc: not a number: '%s'\n", argv[i]);
            return 1;
        }
        if (i == 0 || v < min)
            min = v;
        if (i == 0 || v > max)
            max = v;
        sum += v;
    }
    printf("count=%d min=%ld max=%ld sum=%ld mean=%.2f\n", argc, min, max, sum, argc ? (double)sum / argc : 0.0);
    return 0;
}

static int usage(void) {
    fputs(BENCH_BANNER, stderr);
    fputc('\n', stderr);
    fputs(BENCH_USAGE, stderr);
    return 1;
}

int main(int argc, char **argv) {
    Buffer b;
    int rc;
    const char *cmd;
    if (argc < 3)
        return usage();
    cmd = argv[1];
    if (strcmp(cmd, "stats") == 0)
        return cmd_stats(argc - 2, argv + 2);
    rc = read_file(argv[2], &b);
    if (rc)
        return rc;
    if (strcmp(cmd, "wc") == 0)
        cmd_wc(&b);
    else if (strcmp(cmd, "crc") == 0)
        cmd_crc(&b);
    else if (strcmp(cmd, "rle") == 0)
        cmd_rle(&b);
    else if (strcmp(cmd, "b64") == 0)
        cmd_b64(&b);
    else if (strcmp(cmd, "hist") == 0)
        cmd_hist(&b);
    else if (strcmp(cmd, "sort") == 0)
        cmd_sort(&b);
    else if (strcmp(cmd, "rev") == 0)
        cmd_rev(&b);
    else {
        free(b.data);
        return usage();
    }
    free(b.data);
    return 0;
}
