/* 分机独启（matrix boot）在免费版里被裁成桩，这里补两个入口：
 *
 *   1. pxe_api_add_matrix_boot —— WebUI「添加」按钮调的接口，免费版直接回
 *      {"result":"notsupport"}，不解析请求；
 *   2. toy_matrix_boot_walk —— 启动时恢复配置的回调，免费版只打一行
 *      "Recover matrix boot policy" 日志、不往链表里放东西，所以重启后条目全丢。
 *
 * 其余（list_json / del / save / recovery / proc / init）都是完整实现。
 * 记录布局由 toy_matrix_boot_list_json 反推：
 *   +0x00 u8  bios(index)
 *   +0x04 u32 timeout
 *   +0x08 char mac[18]
 *   +0x1a char path[...]，整条记录 0x101c 字节
 *
 * add 接口的契约与 pxe_api_del_matrix_boot 完全一致：
 *   long handler(void *req, char *out, int *outlen)
 *   req + 0x18 = 解析好的 json 对象；返回值永远 0，响应写进 out。
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
extern void g_free(void *p);
extern int g_ascii_toupper(int c);
extern int g_regex_match_simple(const char *pattern, const char *str, int cflags, int mflags);
extern int g_snprintf(char *buf, usize size, const char *fmt, ...);
extern int strcmp(const char *a, const char *b);
extern void *memcpy(void *dst, const void *src, usize n);
extern usize g_strlcpy(char *dst, const char *src, usize size);

extern void *g_pstEntryList;
extern char g_stMbLock[];

#define ENTRY_SIZE  0x101Cu
#define OFF_IDX     0x00
#define OFF_TIMEOUT 0x04
#define OFF_MAC     0x08
#define OFF_PATH    0x1a
#define MAC_LEN     17
#define MAX_BIOS    6

static usize xstrlen(const char *s)
{
    usize n = 0;
    while (s[n])
        n++;
    return n;
}

static int is_mac_char(u8 c)
{
    return (c >= '0' && c <= '9') || (c >= 'A' && c <= 'F') || c == '*';
}

/* 记录形态：bios 在 0..5、MAC 是 XX-XX-XX-XX-XX-XX、path 非空。
 * 恢复回调的参数顺序上游没写文档，两个参数都可能是记录指针，用这个判定择优。 */
static int looks_like_record(const u8 *p)
{
    int i;

    if (p == 0 || p[OFF_IDX] >= MAX_BIOS || p[OFF_PATH] == 0)
        return 0;
    for (i = 0; i < MAC_LEN; i++) {
        u8 c = p[OFF_MAC + i];
        if (i % 3 == 2) {
            if (c != '-')
                return 0;
        } else if (!is_mac_char(c)) {
            return 0;
        }
    }
    return 1;
}

/* 把一条记录放进全局链表；重复（同 bios 同 MAC）返回 -21，与上游 del 的错误码对齐。 */
static int insert_record(const u8 *rec)
{
    const char *mac = (const char *)(rec + OFF_MAC);
    void *node;
    u8 *entry;

    if (!looks_like_record(rec))
        return -2;

    g_mutex_lock(g_stMbLock);
    node = g_list_first(g_pstEntryList);
    while (node != 0) {
        u8 *e = *(u8 **)node;
        if (e[OFF_IDX] == rec[OFF_IDX] && strcmp((const char *)(e + OFF_MAC), mac) == 0) {
            g_mutex_unlock(g_stMbLock);
            return -21;
        }
        node = *(void **)((u8 *)node + 8);    /* GList.next */
    }
    entry = g_malloc0(ENTRY_SIZE);
    if (entry == 0) {
        g_mutex_unlock(g_stMbLock);
        return -12;
    }
    memcpy(entry, rec, ENTRY_SIZE);
    entry[ENTRY_SIZE - 1] = 0;                /* path 必须有结尾 */
    g_pstEntryList = g_list_append(g_pstEntryList, entry);
    g_mutex_unlock(g_stMbLock);
    return 0;
}

/* 启动恢复回调：把配置里的记录放回链表，否则重启后分机独启列表是空的。
 *
 * 只接受一个参数：上游自己的 toy_matrix_boot_walk 就是 (byte *entry) 这个形态，
 * 多读第二个参数会拿到调用方没设置的寄存器，实测直接段错误（exit 139）。 */
long matrix_boot_walk_hook(void *rec)
{
    if (looks_like_record((const u8 *)rec))
        insert_record((const u8 *)rec);
    return 0;
}

static const char MSG_OK[]    = "{ \"result\" : \"success\" }";
static const char MSG_EXIST[] = "{ \"result\" : \"exist\" }";
static const char MSG_FAIL[]  = "{ \"result\" : \"failed\" }";
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
    u8 *rec;
    int i, rc;

    for (i = 0; i < 32; i++)
        mac[i] = 0;

    if (json == 0)
        goto done;
    if (toy_json_get_int(json, "bios", &bios) != 0)
        goto done;
    if (bios < 0 || bios >= MAX_BIOS)
        goto done;
    if (toy_json_get_uint(json, "timeout", &timeout) != 0 || timeout >= 0xe11)
        goto done;
    toy_json_get_string(json, "mac", 0x20, mac);
    mac[31] = 0;                              /* 上游若没补结尾，避免越界读 */
    if (xstrlen(mac) != MAC_LEN)
        goto done;
    for (i = 0; i < MAC_LEN; i++)
        mac[i] = (char)g_ascii_toupper(mac[i]);
    if (!g_regex_match_simple(MAC_RE, mac, 0, 0))
        goto done;

    path = toy_json_get_string_ex(json, "path");
    if (path == 0 || path[0] == 0)
        goto done;

    rec = g_malloc0(ENTRY_SIZE);
    if (rec == 0)
        goto done;
    rec[OFF_IDX] = (u8)bios;
    *(u32 *)(rec + OFF_TIMEOUT) = timeout;
    for (i = 0; i < MAC_LEN; i++)
        rec[OFF_MAC + i] = (u8)mac[i];
    g_strlcpy((char *)(rec + OFF_PATH), path, ENTRY_SIZE - OFF_PATH);
    rc = insert_record(rec);
    g_free(rec);
    if (rc == 0)
        result = MSG_OK;
    else if (rc == -21)
        result = MSG_EXIST;

done:
    *outlen = g_snprintf(out, 0x800000, "%s", result);
    return 0;
}
