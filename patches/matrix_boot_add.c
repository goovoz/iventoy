/* 分机独启（matrix boot）add 接口 —— 免费版被编译成了桩，这里补一个等价实现。
 *
 * 约定与 pxe_api_del_matrix_boot 完全一致：
 *   long handler(void *req, char *out, int *outlen)
 *   req + 0x18 = 解析好的 json 对象
 * 返回值永远 0，响应内容写进 out，长度写进 *outlen。
 *
 * 记录结构（由 toy_matrix_boot_list_json 反推）：
 *   +0x00 u8  bios(index)
 *   +0x04 u32 timeout
 *   +0x08 char mac[18]
 *   +0x1a char path[...]，整条记录 0x101c 字节
 */
typedef unsigned char u8;
typedef unsigned int u32;
typedef unsigned long usize;

extern int toy_json_get_int(void *json, const char *key, int *out);
extern int toy_json_get_uint(void *json, const char *key, u32 *out);
extern int toy_json_get_string(void *json, const char *key, int max, char *buf);
extern char *toy_json_get_string_ex(void *json, const char *key);

extern void *g_list_first(void *list);
extern void *g_list_append(void *list, void *data);
extern void g_mutex_lock(void *lock);
extern void g_mutex_unlock(void *lock);
extern void *g_malloc0(usize n);
extern int g_ascii_toupper(int c);
extern int g_regex_match_simple(const char *pattern, const char *str, int cflags, int mflags);
extern int g_snprintf(char *buf, usize size, const char *fmt, ...);
extern int strcmp(const char *a, const char *b);
extern void *memcpy(void *dst, const void *src, usize n);
extern usize g_strlcpy(char *dst, const char *src, usize size);

extern void *g_pstEntryList;
extern char g_stMbLock[];

#define ENTRY_SIZE 0x101Cu
#define OFF_IDX    0x00
#define OFF_TIMEOUT 0x04
#define OFF_MAC    0x08
#define OFF_PATH   0x1a

static usize xstrlen(const char *s)
{
    usize n = 0;
    while (s[n])
        n++;
    return n;
}

static int insert_entry(u8 bios, const char *mac, const char *path, u32 timeout)
{
    void *node;
    u8 *entry;

    if (bios >= 6 || mac == 0 || path == 0)
        return -2;
    if (xstrlen(mac) != 17)
        return -2;

    g_mutex_lock(g_stMbLock);
    node = g_list_first(g_pstEntryList);
    while (node != 0) {
        u8 *e = *(u8 **)node;
        if (e[OFF_IDX] == bios && strcmp((const char *)(e + OFF_MAC), mac) == 0) {
            g_mutex_unlock(g_stMbLock);
            return -21;                       /* 已存在 -> "exist" */
        }
        node = *(void **)((u8 *)node + 8);    /* GList.next */
    }
    entry = g_malloc0(ENTRY_SIZE);
    if (entry == 0) {
        g_mutex_unlock(g_stMbLock);
        return -12;
    }
    entry[OFF_IDX] = bios;
    *(u32 *)(entry + OFF_TIMEOUT) = timeout;
    memcpy(entry + OFF_MAC, mac, 18);
    g_strlcpy((char *)(entry + OFF_PATH), path, ENTRY_SIZE - OFF_PATH);
    g_pstEntryList = g_list_append(g_pstEntryList, entry);
    g_mutex_unlock(g_stMbLock);
    return 0;
}

static const char MSG_OK[]   = "{ \"result\" : \"success\" }";
static const char MSG_EXIST[] = "{ \"result\" : \"exist\" }";
static const char MSG_FAIL[] = "{ \"result\" : \"failed\" }";
static const char MAC_RE[] =
    "^[0-9A-F*]{2}[-][0-9A-F*]{2}[-][0-9A-F*]{2}[-][0-9A-F*]{2}[-][0-9A-F*]{2}[-][0-9A-F*]{2}$";

long matrix_boot_add_api(void *req, char *out, int *outlen)
{
    void *json = *(void **)((u8 *)req + 0x18);
    const char *result = MSG_FAIL;
    char mac[32];
    int bios = -1;
    u32 timeout = 0;
    const char *path;
    int rc;
    int i;

    for (i = 0; i < 32; i++)
        mac[i] = 0;

    if (toy_json_get_int(json, "bios", &bios) != 0)
        goto done;
    if (toy_json_get_uint(json, "timeout", &timeout) != 0 || timeout >= 0xe11)
        goto done;

    toy_json_get_string(json, "mac", 0x20, mac);
    if (xstrlen(mac) != 17)
        goto done;
    for (i = 0; i < 17; i++)
        mac[i] = (char)g_ascii_toupper(mac[i]);
    if (!g_regex_match_simple(MAC_RE, mac, 0, 0))
        goto done;

    path = toy_json_get_string_ex(json, "path");
    if (path == 0)
        goto done;

    rc = insert_entry((u8)bios, mac, path, timeout);
    if (rc == 0)
        result = MSG_OK;
    else if (rc == -21)
        result = MSG_EXIST;

done:
    *outlen = g_snprintf(out, 0x800000, "%s", result);
    return 0;
}
