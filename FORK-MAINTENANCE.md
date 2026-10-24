# ndj888/unit 自维护路线

本文件是 ndj888/unit 分支的维护手册，也是交给 Codex 等执行方的背景说明。
官方 nginx/unit 已于 2025 年 10 月归档，最后一个提交是 `76b39e92`。

## 1. 定位

- **上游**：社区 LTS 分支 [FreeUnit](https://github.com/freeunitorg/freeunit)（Apache-2.0）。
  它与本仓库共享官方历史，merge-base 正是 `76b39e92`，所以同步方式是 `git merge` 它的 release tag。
  从 1.37.0 起，本仓库以 FreeUnit 的发布为同步单位。
- **分支自有补丁**（越少越好，每个都要能说清为什么不在上游）：

  | 补丁 | 文件 | 首次发布 | 上游状态 |
  |---|---|---|---|
  | PHP `UNIT_REWRITTEN_PATH`（nginx/unit#1615） | `src/nxt_php_sapi.c`、`test/test_php_application.py`、`test/php/query_string/index.php` | ndj888 1.36.0 | 待提交给 FreeUnit |

- **从 FreeUnit master 提前移植的提交**（FreeUnit 下次发版时会自然并入，同步后从表中删除）：

  | 上游提交 | 内容 | 原因 |
  |---|---|---|
  | aae35c6f | 共享内存段大小校验从“必须等于”改为“不得小于” | 1.37.0 在 Apple Silicon macOS 上所有应用请求都失败（freeunitorg/freeunit#595） |
  | 85a8d49c | 让 C 测试能在 macOS 上编译运行，并新增 `build-test-macos.yml` | 同上，本地 macOS 验收需要 |
  | 872bf041 | macOS 上统计 shm 映射数量的测试 | 同上 |

- **领先 FreeUnit 的依赖升级**（1.37.0 发布后才公布的 RustSec 公告，FreeUnit master 截至 2026-10-24 还没跟进）：
  wasmtime 48.0.5 → 48.0.7、tokio → 1.54.1、rustls → 0.23.46、rustls-webpki → 0.103.16、webpki-roots → 1.0.10。
  contrib 的 wasmtime 源码包改为从官方 GitHub release 下载（`pkg/contrib/src/wasmtime/Makefile`、`audit.yml`），
  因为 FreeUnit 的镜像 `packages.freeunit.org` 上没有 48.0.7。
  `pkg/contrib/wasmtime-lock-audit.allow.json` 中 tokio 1.51.1（RUSTSEC-2026-0386，属于 unsound 类）的例外只适用于 48.0.7 的 vendored 锁文件，下次升级 wasmtime 时必须复审。
  同步 FreeUnit 时，这些文件很可能冲突：取两边中**较新**的版本，并重新运行 `cargo audit`。

- **分支适配文件**（每次同步都要保留）：`README.md` 顶部的 "About this fork"、`SECURITY.md` 顶部一节、
  4 个 workflow 中的 `if: github.repository == 'freeunitorg/freeunit'` 防护
  （`release-docker.yml` 的 setup、`build-deb.yml` 的 release、`unitctl.yml` 的 release、`claude.yml`），
  以及本仓库自己的 release 资产 workflow 和本文件。
- **版本号**：与 FreeUnit 对齐。同步 FreeUnit `x.y.z` 就发布 `x.y.z`。
  两次同步之间只修分支补丁时，tag 用 `x.y.z-N`（`NXT_VERSION` 不变），并在 Release 说明里写清楚。

## 2. 节奏

| 频率 | 做什么 | 怎么触发 |
|---|---|---|
| 每周一（自动） | `audit.yml`：所有 `Cargo.lock` 对照 RustSec，`pkg/contrib` 的源码包对照 OSV；`eol-check.yml` 和 `build-distros.yml` 有问题时会在本仓库开 issue | GitHub Actions 定时任务（只在默认分支 master 上运行） |
| 每周 | 查 FreeUnit 有无新 release 或安全公告（命令见第 4 节） | 手动，或用 Claude 定时任务 |
| FreeUnit 每次发布（大约每月一次） | 按第 3 节同步，发布同号版本 | 看到新 tag |
| FreeUnit 发布安全公告 | **72 小时内**同步并发布 | 公告 |
| 每季度 | 复查第 6 节待办；检查 FreeUnit 是否仍活跃 | 手动 |

**上游失效判定**：FreeUnit 连续 90 天没有提交，或者一个高危公告 30 天内没有修复版本，就切换到第 5 节的"自主修复模式"。

## 3. 同步 FreeUnit 新版本（标准流程）

```bash
# 一次性设置（已完成）：remote 不拉 tag，避免和本仓库同名 tag 冲突
git remote add freeunit https://github.com/freeunitorg/freeunit.git
git config remote.freeunit.tagOpt --no-tags

# 每次同步，X.Y.Z 换成 FreeUnit 的版本
git checkout -b release/X.Y.Z master
git fetch freeunit refs/tags/X.Y.Z:refs/remotes/freeunit/X.Y.Z
git merge --no-commit --no-ff 'freeunit/X.Y.Z^{commit}'
```

预期冲突文件和处理办法（1.37.0 的实际情况）：

- `version`、`docs/unit-openapi.yaml`：取 FreeUnit 一侧。
- `CHANGES`、`docs/changes.xml`：以 FreeUnit 为准，把分支补丁的条目放进新版本段。
- `README.md`、`SECURITY.md`、4 个 workflow：保留分支适配，其余跟随上游。

注意事项：

- Codex 的沙箱禁止创建 POSIX 共享内存，在沙箱里 `./configure` 会报 "no shared memory implementation found"。编译和测试要在沙箱外运行，由 Claude 验收时执行。
- 跨版本构建前先清掉 `build/`：旧的依赖文件会引用已经删除的源文件，比如 1.37.0 删掉了 `src/nxt_fiber.h`。
- 如果 FreeUnit 的 tag 之后在 master 上又修了严重问题（比如 1.37.0 的 #595），用 `git cherry-pick -x <sha>` 移植，记进第 1 节的表格，CHANGES 条目写明 "Backported … from FreeUnit master"。

然后用 `/codex-do` 交给 Codex。任务说明可以参照 1.37.0 那份，验收标准至少包括：

1. 没有冲突标记。
2. `git diff 'freeunit/X.Y.Z^{commit}' --stat` 只涉及第 1 节列出的补丁和适配文件。
3. `git diff 'freeunit/X.Y.Z^{commit}' -- src/` 只有分支补丁。
4. macOS 本机：`./configure --openssl --zlib --zstd --brotli --tests …`，`make && make tests && build/tests`；python 模块编译通过，pytest 子集通过。
5. `trivy fs --scanners vuln .` 为 0。
6. 推送 `release/X.Y.Z` 分支并开 PR 到 master，GitHub CI 全绿：上游的 build-test、sanitize 等，加上本仓库的 release 资产构建，它会在 Linux 上编译 PHP 模块并运行 `test_php_application.py`。

最后合并 PR，打 tag `X.Y.Z`，再用 `gh release create` 发布。Release 资产由 workflow 自动上传，发布后核对 sha256。

## 4. 安全情报来源

```bash
# FreeUnit 安全公告（含受影响版本和修复版本）
gh api repos/freeunitorg/freeunit/security-advisories \
  --jq '.[] | [.ghsa_id, .cve_id, .severity, .published_at, .summary] | @tsv'
# FreeUnit 最新 release
gh release list -R freeunitorg/freeunit --limit 5
# 本地依赖扫描（Cargo.lock / go.mod / requirements 等）
trivy fs --scanners vuln .
```

另外需要人工关注：

- NVD 和 F5 公告中关于 "NGINX Unit" 的条目（历史上只有 CVE-2019-7401 和 CVE-2025-1695，均已修复）。
- 不在锁文件里的依赖：Java 模块下载的 Tomcat jar（`auto/modules/java` 中的 `NXT_TOMCAT_VERSION`，对照 Tomcat 9 安全页面）、`pkg/contrib` 中的 njs、wasmtime 和 wasi-sysroot（`audit.yml` 已覆盖 wasmtime）、系统 OpenSSL 和 PCRE2（由发行版负责）。
- 供应链：FreeUnit 把 contrib 源码包镜像改到了 `packages.freeunit.org`。同步时检查 `pkg/contrib/src/*/SHA512SUMS` 有没有被无故修改。

## 5. 自主修复模式（上游失效时）

1. 一个漏洞对应一个 Codex 任务说明：先写复现测试（pytest，或 `src/test/` 下的 C 单测），再修复。验收必须包括 sanitize.yml（ASan/UBSan）通过。
2. 涉及 HTTP 解析、IPC 或共享内存的修复，要同时补 fuzzing 目标（`fuzzing/`，CI 中有 `fuzzing.yml`）。
3. 在 https://github.com/ndj888/unit/security/advisories 起草公告。需要 CVE 时，通过 GitHub（CNA）申请。修复版本发布后再公开。
4. 修复同时提交给 FreeUnit（如果它还在），避免两边各修各的。

## 6. 待办

- [ ] 把 `UNIT_REWRITTEN_PATH` 补丁提交到 FreeUnit 上游。被接受后，本分支就只剩同步和发布。
- [ ] njs 升级到 1.0.2 或更高版本（`pkg/contrib/src/njs/version`，目前是 1.0.1）。CVE-2026-101025 和 CVE-2026-78689 都在 njs 的 XML 模块里，而 contrib 构建 njs 时用了 `--no-libxml2`，所以目前不受影响；等 FreeUnit 升级后随同步一起更新即可。
- [ ] FreeUnit 发布 1.37.1 后同步，第 1 节表格里的 3 个移植提交会被它覆盖。
- [ ] macOS 已知问题（旧问题，1.36.0 就有）：应用向一个已满的 AF_UNIX 数据报 socket 发消息时，macOS 返回 `ENOBUFS`，Linux 返回 `EAGAIN`。libunit 把 `ENOBUFS` 当成错误，大请求体的 Python 应用（`test_python_application_input_readlines_huge`）会卡住。Linux 不受影响。FreeUnit master 的 05b18e7d 只在非阻塞的唤醒消息上处理了 `ENOBUFS`。等 FreeUnit 下一版发布后复测；如果仍然存在，就向 FreeUnit 报告，并附上这个复现用例。
- [ ] macOS 已知问题（1.37.0 发现，尚未定位）：`test_python_compression_concurrent_streams[zstd]` 在 macOS arm64 上 24 个响应全部超时；gzip 和 deflate 正常，Linux CI 的 python 3.12/3.13/3.14 全部通过。卡住时用 `sample <应用 pid>` 抓栈，可以看到应用的 32 个线程一部分在 libunit 的互斥锁上等待，一部分在 `take_gil` 里等待，疑似 GIL 和 libunit 锁的顺序问题在 macOS 上被触发。复现命令：`pytest test/test_python_compression.py -k "concurrent_streams and zstd"`。确认原因后向 FreeUnit 报告。
- [ ] 跟踪 FreeUnit 尚未关闭的安全问题：freeunitorg/freeunit#343（没有完成的分片消息会一直占着文件描述符，1.37.0 已把每个 port 的上限设为 64 个）。
- [ ] 复核 1.36.0 时记下的上游问题在 FreeUnit 里的状态：nginx/unit#1626（流式响应下的 `limits.timeout`，FreeUnit 1.37.0 已部分修改相关逻辑）、#1653（macOS 上 Python ASGI 段错误）、#1370（RHEL 8/9 Python 包）。
- [ ] 名称与商标："NGINX" 是 F5 的商标。建议以后的 Release 标题用 "Unit (ndj888 build)" 之类的名称，避免看起来像官方发布。
- [ ] 视需要补 Docker 镜像发布：上游的 `release-docker.yml` 已在本仓库禁用，需要时另写一个推送到 `ghcr.io/ndj888/unit` 的 workflow。

## 7. 1.37.0 升级须知（相对官方 1.35.0 和 ndj888 1.36.0 的行为变化）

以下变化来自 FreeUnit 1.35.1 到 1.37.0，详见 `CHANGES`：

- 控制 socket 只接受 root 或 unitd 自身有效 UID 的连接，不再只靠文件权限。
- 访问日志：变量值中的引号、反斜杠、控制字符和非 ASCII 字节会转义为 `\xHH`；对象格式的成员名不再展开变量。
- 路由正则：单次匹配最多 100,000 步，超出时返回 500；用 PCRE1 或 PCRE2 10.30 之前的版本时，递归深度上限为 2,000。
- 非 root 运行的 unitd 不再把 capability 传给应用；`credential` 命名空间里的应用如果需要 capability，必须配置 `"user": "root"`。
- HTTP keep-alive 的 `idle_timeout` 默认值从 180 秒改为 30 秒。
- `limits.timeout` 现在也限制请求等待应用进程的时间。
- WebAssembly component 模块禁止 guest 主动发起出站 HTTP；wasmtime 升级到 48.0.5。
- 编译需要 OpenSSL 1.1.1 或更高版本；Java 模块使用 Tomcat 9.0.122；OTel 的默认服务名改为 "FreeUnit"。
- njs 模块名不能以 "." 开头，单个模块上限 16 MiB。
