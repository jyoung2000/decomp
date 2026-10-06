/*
 * pecli - tiny key/value store CLI with its own binary save format.
 * Ground-truth source for the Rebuild Studio "pecli" fixture.
 *
 * File format (all integers little-endian):
 *   offset 0  : magic "PCLI" (4 bytes)
 *   offset 4  : u32 version (currently 1)
 *   offset 8  : u32 record count
 *   offset 12 : u32 CRC32 (IEEE 802.3, reflected, poly 0xEDB88320) over the record area
 *   offset 16 : records, each: u16 key_len, u16 value_len, key bytes, value bytes
 *
 * Exit codes:
 *   0 ok, 1 usage / key not found / other logical failure,
 *   2 bad magic or unsupported version, 3 file missing/unreadable,
 *   4 corrupt file (crc mismatch / truncated records), 5 write failure
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#define MAGIC "PCLI"
#define VERSION 1u
#define HEADER_SIZE 16
#define MAX_KEY 64
#define MAX_VALUE 1024
#define MAX_RECORDS 256

#define EXIT_OK 0
#define EXIT_USAGE 1
#define EXIT_BADMAGIC 2
#define EXIT_NOFILE 3
#define EXIT_CORRUPT 4
#define EXIT_WRITE 5

typedef struct {
    char key[MAX_KEY + 1];
    char value[MAX_VALUE + 1];
} Record;

typedef struct {
    uint32_t count;
    Record rec[MAX_RECORDS];
} Store;

static Store g_store;

static uint32_t crc32_update(uint32_t crc, const uint8_t *buf, size_t len)
{
    size_t i;
    int b;
    crc = ~crc;
    for (i = 0; i < len; i++) {
        crc ^= buf[i];
        for (b = 0; b < 8; b++)
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
    }
    return ~crc;
}

static void put16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}
static uint16_t get16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/* Serialise all records into a freshly allocated buffer; returns length. */
static size_t pack_records(const Store *s, uint8_t **out)
{
    size_t total = 0, pos = 0;
    uint32_t i;
    uint8_t *buf;
    for (i = 0; i < s->count; i++)
        total += 4 + strlen(s->rec[i].key) + strlen(s->rec[i].value);
    buf = (uint8_t *)malloc(total ? total : 1);
    if (!buf) { fprintf(stderr, "error: out of memory\n"); exit(EXIT_WRITE); }
    for (i = 0; i < s->count; i++) {
        size_t kl = strlen(s->rec[i].key), vl = strlen(s->rec[i].value);
        put16(buf + pos, (uint16_t)kl);
        put16(buf + pos + 2, (uint16_t)vl);
        memcpy(buf + pos + 4, s->rec[i].key, kl);
        memcpy(buf + pos + 4 + kl, s->rec[i].value, vl);
        pos += 4 + kl + vl;
    }
    *out = buf;
    return total;
}

static int save_store(const char *path, const Store *s)
{
    uint8_t hdr[HEADER_SIZE];
    uint8_t *recs;
    size_t len = pack_records(s, &recs);
    FILE *f = fopen(path, "wb");
    if (!f) {
        fprintf(stderr, "error: cannot write '%s'\n", path);
        free(recs);
        return EXIT_WRITE;
    }
    memcpy(hdr, MAGIC, 4);
    put32(hdr + 4, VERSION);
    put32(hdr + 8, s->count);
    put32(hdr + 12, crc32_update(0, recs, len));
    if (fwrite(hdr, 1, HEADER_SIZE, f) != HEADER_SIZE ||
        (len && fwrite(recs, 1, len, f) != len)) {
        fprintf(stderr, "error: short write to '%s'\n", path);
        fclose(f);
        free(recs);
        return EXIT_WRITE;
    }
    fclose(f);
    free(recs);
    return EXIT_OK;
}

/* Loads path into g_store. Returns 0 or the exit code to use. */
static int load_store(const char *path)
{
    uint8_t hdr[HEADER_SIZE];
    uint8_t *body = NULL;
    long size;
    size_t body_len, pos = 0;
    uint32_t i, count, crc;
    FILE *f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "error: file not found: %s\n", path);
        return EXIT_NOFILE;
    }
    if (fread(hdr, 1, HEADER_SIZE, f) != HEADER_SIZE || memcmp(hdr, MAGIC, 4) != 0) {
        fprintf(stderr, "error: bad magic in %s (not a PCLI file)\n", path);
        fclose(f);
        return EXIT_BADMAGIC;
    }
    if (get32(hdr + 4) != VERSION) {
        fprintf(stderr, "error: unsupported version %u in %s\n", (unsigned)get32(hdr + 4), path);
        fclose(f);
        return EXIT_BADMAGIC;
    }
    count = get32(hdr + 8);
    crc = get32(hdr + 12);
    fseek(f, 0, SEEK_END);
    size = ftell(f);
    body_len = (size > HEADER_SIZE) ? (size_t)(size - HEADER_SIZE) : 0;
    fseek(f, HEADER_SIZE, SEEK_SET);
    body = (uint8_t *)malloc(body_len ? body_len : 1);
    if (!body || (body_len && fread(body, 1, body_len, f) != body_len)) {
        fprintf(stderr, "error: cannot read records from %s\n", path);
        fclose(f);
        free(body);
        return EXIT_NOFILE;
    }
    fclose(f);
    if (crc32_update(0, body, body_len) != crc) {
        fprintf(stderr, "error: checksum mismatch in %s (file is corrupt)\n", path);
        free(body);
        return EXIT_CORRUPT;
    }
    if (count > MAX_RECORDS) {
        fprintf(stderr, "error: too many records (%u) in %s\n", (unsigned)count, path);
        free(body);
        return EXIT_CORRUPT;
    }
    g_store.count = 0;
    for (i = 0; i < count; i++) {
        uint16_t kl, vl;
        if (pos + 4 > body_len) goto truncated;
        kl = get16(body + pos);
        vl = get16(body + pos + 2);
        if (kl > MAX_KEY || vl > MAX_VALUE || pos + 4 + kl + vl > body_len) goto truncated;
        memcpy(g_store.rec[i].key, body + pos + 4, kl);
        g_store.rec[i].key[kl] = 0;
        memcpy(g_store.rec[i].value, body + pos + 4 + kl, vl);
        g_store.rec[i].value[vl] = 0;
        pos += 4 + kl + vl;
        g_store.count++;
    }
    free(body);
    return EXIT_OK;
truncated:
    fprintf(stderr, "error: truncated or malformed record %u in %s\n", (unsigned)i, path);
    free(body);
    return EXIT_CORRUPT;
}

static int find_key(const char *key)
{
    uint32_t i;
    for (i = 0; i < g_store.count; i++)
        if (strcmp(g_store.rec[i].key, key) == 0) return (int)i;
    return -1;
}

static int usage(void)
{
    fprintf(stderr,
        "usage: pecli <command> <file> [args]\n"
        "  init <file>               create an empty store\n"
        "  add <file> <key> <value>  add or replace a record\n"
        "  get <file> <key>          print a record value\n"
        "  list <file>               list all records\n"
        "  remove <file> <key>       delete a record\n"
        "  checksum <file>           print stored and computed CRC32\n");
    return EXIT_USAGE;
}

static int cmd_init(const char *path)
{
    int rc;
    g_store.count = 0;
    rc = save_store(path, &g_store);
    if (rc == EXIT_OK) printf("initialized %s (0 records)\n", path);
    return rc;
}

static int cmd_add(const char *path, const char *key, const char *value)
{
    int rc, idx;
    if (strlen(key) == 0 || strlen(key) > MAX_KEY || strlen(value) > MAX_VALUE) {
        fprintf(stderr, "error: key must be 1..%d chars and value at most %d chars\n", MAX_KEY, MAX_VALUE);
        return EXIT_USAGE;
    }
    rc = load_store(path);
    if (rc) return rc;
    idx = find_key(key);
    if (idx >= 0) {
        strcpy(g_store.rec[idx].value, value);
        rc = save_store(path, &g_store);
        if (rc == EXIT_OK) printf("updated %s\n", key);
        return rc;
    }
    if (g_store.count >= MAX_RECORDS) {
        fprintf(stderr, "error: store is full\n");
        return EXIT_USAGE;
    }
    strcpy(g_store.rec[g_store.count].key, key);
    strcpy(g_store.rec[g_store.count].value, value);
    g_store.count++;
    rc = save_store(path, &g_store);
    if (rc == EXIT_OK) printf("added %s (%u records)\n", key, (unsigned)g_store.count);
    return rc;
}

static int cmd_get(const char *path, const char *key)
{
    int rc = load_store(path), idx;
    if (rc) return rc;
    idx = find_key(key);
    if (idx < 0) {
        fprintf(stderr, "error: key not found: %s\n", key);
        return EXIT_USAGE;
    }
    printf("%s\n", g_store.rec[idx].value);
    return EXIT_OK;
}

static int cmd_list(const char *path)
{
    uint32_t i;
    int rc = load_store(path);
    if (rc) return rc;
    printf("%u records\n", (unsigned)g_store.count);
    for (i = 0; i < g_store.count; i++)
        printf("%s=%s\n", g_store.rec[i].key, g_store.rec[i].value);
    return EXIT_OK;
}

static int cmd_remove(const char *path, const char *key)
{
    uint32_t i;
    int rc = load_store(path), idx;
    if (rc) return rc;
    idx = find_key(key);
    if (idx < 0) {
        fprintf(stderr, "error: key not found: %s\n", key);
        return EXIT_USAGE;
    }
    for (i = (uint32_t)idx; i + 1 < g_store.count; i++)
        g_store.rec[i] = g_store.rec[i + 1];
    g_store.count--;
    rc = save_store(path, &g_store);
    if (rc == EXIT_OK) printf("removed %s (%u records)\n", key, (unsigned)g_store.count);
    return rc;
}

static int cmd_checksum(const char *path)
{
    uint8_t *recs;
    size_t len;
    uint32_t computed;
    uint8_t hdr[HEADER_SIZE];
    FILE *f;
    int rc = load_store(path);
    if (rc) return rc;
    f = fopen(path, "rb");
    if (!f || fread(hdr, 1, HEADER_SIZE, f) != HEADER_SIZE) {
        fprintf(stderr, "error: file not found: %s\n", path);
        if (f) fclose(f);
        return EXIT_NOFILE;
    }
    fclose(f);
    len = pack_records(&g_store, &recs);
    computed = crc32_update(0, recs, len);
    free(recs);
    printf("stored:   %08x\n", (unsigned)get32(hdr + 12));
    printf("computed: %08x\n", (unsigned)computed);
    printf("records:  %u\n", (unsigned)g_store.count);
    return EXIT_OK;
}

int main(int argc, char **argv)
{
    const char *cmd;
    if (argc < 3) return usage();
    cmd = argv[1];
    if (strcmp(cmd, "init") == 0 && argc == 3) return cmd_init(argv[2]);
    if (strcmp(cmd, "add") == 0 && argc == 5) return cmd_add(argv[2], argv[3], argv[4]);
    if (strcmp(cmd, "get") == 0 && argc == 4) return cmd_get(argv[2], argv[3]);
    if (strcmp(cmd, "list") == 0 && argc == 3) return cmd_list(argv[2]);
    if (strcmp(cmd, "remove") == 0 && argc == 4) return cmd_remove(argv[2], argv[3]);
    if (strcmp(cmd, "checksum") == 0 && argc == 3) return cmd_checksum(argv[2]);
    return usage();
}
