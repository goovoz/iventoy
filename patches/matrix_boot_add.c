/* 分机独启（matrix boot）在免费版里被裁成桩，这里补两个入口：
 *
 *   1. pxe_api_add_matrix_boot —— WebUI「添加」按钮调的接口，免费版直接回
 *      {"result":"notsupport"}，不解析请求；
 *   2. toy_matrix_boot_walk —— 启动时恢复配置的回调，免费版只打一行
 *      "Recover matrix boot policy" 日志、不往链表里放东西，所以重启后条目全丢。
 *
 * 另外接管 toy_util_buf2file，把配置落盘串行化（见文件末尾）：它非原子，
 * 两个线程同时写 config.dat 会写出半截文件，加载侧校验不过就整份丢弃。
 *
 * 最后一个入口不是函数替换而是调用点重定向：toy_tarfs_find_file 的调用方全改到
 * tarfs_find_hook，用来在内存里拆掉 WebUI 的「专业版本」前台闸门（见文件末尾）。
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

/* ---- 配置落盘串行化 ------------------------------------------------------
 *
 * toy_util_buf2file 的写法是 open(path, O_WRONLY|O_CREAT|O_TRUNC) 之后一次
 * write，非原子。它只有两个调用者：toy_log_thread_main（日志）和
 * toy_pxe_cfg_save（写 data/config.dat），后者被这些线程/时机调用：
 *
 *   http_api_main_proc   —— HTTP API 处理完一次请求之后
 *   main_stop            —— 进程收到 SIGTERM 的关闭流程
 *   pxe_api_thread_main  —— 服务启动/停止/镜像刷新
 *
 * 重启时「API 线程的保存」和「关闭流程的保存」会撞在同一个文件上：A 先截断，
 * B 正写到一半，落盘结果就是半截。加载侧 toy_pxe_cfg_chk_data 用
 * toy_util_nbd_pkt_filt 校验头 4 字节，对不上就整份丢弃：
 *
 *     config data corrupted, ignore it. [0xaf74fd4e 0x169679e6 2271]
 *
 * 后果不是丢一个条目，而是所有设置回默认、分机独启列表空掉，且不报错。
 * 这里给写盘加 flock 串行：锁的是目标文件本身（不额外产生锁文件），
 * 关闭 fd 即解锁。第二个写者必须等第一个写完，写出来的永远是完整文件。
 *
 * 语义按上游原样保留：第 4 个参数为 0 → O_WRONLY|O_CREAT|O_TRUNC，
 * 非 0 → O_WRONLY|O_APPEND。两个现行调用点传的都是 0。
 */
extern int open64(const char *path, int flags, int mode);
extern long write(int fd, const void *buf, unsigned long n);
extern int close(int fd);
extern int flock(int fd, int op);
extern int toy_log(int level, const char *mod, const char *fmt, ...);

#define O_WRITE_  00000001
#define O_CREAT_  00000100
#define O_TRUNC_  00001000
#define O_APPEND_ 00002000
#define LOCK_EX_  2
#define LOCK_UN_  8

long buf2file_hook(void *buf, int len, const char *path, int append)
{
    int lockfd;
    int fd;
    long n;

    if (path == 0) {
        toy_log(2, "UTIL", "buf2file: null path");
        return -1;
    }

    lockfd = open64(path, O_WRITE_ | O_CREAT_, 0644);
    if (lockfd >= 0)
        flock(lockfd, LOCK_EX_);

    fd = open64(path, append ? (O_WRITE_ | O_APPEND_)
                             : (O_WRITE_ | O_CREAT_ | O_TRUNC_), 0644);
    if (fd < 0) {
        toy_log(2, "UTIL", "open <%s> for write failed", path);
        if (lockfd >= 0) {
            flock(lockfd, LOCK_UN_);
            close(lockfd);
        }
        return -1;
    }

    n = write(fd, buf, (unsigned long)len);
    close(fd);
    if (lockfd >= 0) {
        flock(lockfd, LOCK_UN_);
        close(lockfd);
    }
    if (n != (long)len) {
        toy_log(2, "UTIL", "write <%s> short: %ld/%d", path, n, len);
        return -1;
    }
    return 0;
}

/* ---- 免费版 UI 闸门：分机独启的三个按钮 ---------------------------------
 *
 * WebUI 的 matrix-boot.html 里三处：
 *     if (g_current_edition === 0) { Message.error(STR_EDITION_NOT_SUPPORT); return; }
 * 也就是「新增 / 清除 / 导入」在免费版里根本没走到接口 —— 接口补好了也点不动。
 * 而 g_current_edition 全站只有一处赋值，在 index.html：
 *     g_current_edition = 0;
 *
 * 页面资源打包在 data/iventoy.dat 里：tar 包，每 4 字节做过字节翻转，外面套
 * 0x80 字节 RSA 签名，toy_tarfs_init 用内嵌公钥验签，签名不过就整份不加载。
 * 所以改盘上的 .dat 会直接毁掉 WebUI；改成在 tarfs 解压后的内存缓冲里把那一个
 * 字节换掉，磁盘不动、签名也不动。
 *
 * 触发点：脚本把 toy_tarfs_find_file 的所有调用点重定向到这里（按 VA 重算
 * x86 的 rel32 / arm64 的 bl imm26），只在请求 .html 时扫一遍缓冲，改完照原样
 * 调原函数，行为其余部分完全不变。
 */
extern void *g_pucTarfsBuf;
extern int g_iTarfsLen;
extern void *toy_tarfs_find_file(const char *path, void *size_out);
extern int memcmp(const void *a, const void *b, usize n);

#define EDITION_PAT "g_current_edition = 0"
#define WORD_G      0x6767676767676767UL      /* 'g' x8 */
#define WORD_ONE    0x0101010101010101UL
#define WORD_HIGH   0x8080808080808080UL

static int str_has(const char *s, const char *needle)
{
    usize i;
    usize j;

    for (i = 0; s[i]; i++) {
        for (j = 0; needle[j] && s[i + j] == needle[j]; j++)
            ;
        if (needle[j] == 0)
            return 1;
    }
    return 0;
}

/* 缓冲有几十 MB，逐字节走太慢；先按 8 字节字跳过不含 'g' 的块（memchr 在这个
 * 二进制里没被导入，只能自己写）。只在请求 .html 时调用，一次几毫秒。 */
static void patch_ui_edition(const char *path)
{
    unsigned char *buf = (unsigned char *)g_pucTarfsBuf;
    unsigned char *p;
    unsigned char *end;
    int len = g_iTarfsLen;
    int hits = 0;
    usize patlen = sizeof(EDITION_PAT) - 1;

    if (buf == 0 || len <= (int)patlen + 8 || path == 0)
        return;
    if (!str_has(path, ".html"))
        return;

    end = buf + len;
    p = buf;
    while (p + patlen <= end) {
        while (p + 8 <= end) {
            unsigned long w = 0;
            unsigned long x;

            memcpy(&w, p, 8);
            x = w ^ WORD_G;
            if (((x - WORD_ONE) & ~x & WORD_HIGH) != 0)
                break;                          /* 这 8 字节里有 'g'，细看 */
            p += 8;
        }
        if (p + patlen > end)
            break;
        if (memcmp(p, EDITION_PAT, patlen) == 0) {
            p[patlen - 1] = '1';                /* 末尾的 '0' -> '1' */
            hits++;
        }
        p++;
    }
    if (hits > 0)
        toy_log(3, "HOOK", "matrix boot: unlocked %d web UI edition gate(s)", hits);
}

void *tarfs_find_hook(const char *path, void *size_out)
{
    patch_ui_edition(path);
    return toy_tarfs_find_file(path, size_out);
}

/* ---- 「清除」按钮 ---------------------------------------------------------
 * pxe_api_clr_matrix_boot 也是桩（跟 add 一样只回 notsupport），但数据层
 * toy_matrix_boot_clear_all 是完整实现：加锁、g_list_free_full、置空链表。
 * 这里补一个完整处理器，语义照 list/del 那一套：写 out 并返回 0；
 * 配置落盘由分派器在 flags&1 的分支里统一做（见 http_api_main_proc 0x44D7E9）。
 */
extern int toy_matrix_boot_clear_all(void);

long matrix_boot_clr_api(void *req, char *out, int *outlen)
{
    (void)req;
    toy_matrix_boot_clear_all();
    toy_log(3, "HOOK", "matrix boot: cleared all entries");
    *outlen = g_snprintf(out, 0x800000, "%s", MSG_OK);
    return 0;
}
