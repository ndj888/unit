# ndj888/unit 自维护路线

本文件是 ndj888/unit 分支的维护手册，也是交给 Codex 等执行方的背景说明。
官方 nginx/unit 已于 2025 年 10 月归档，最后一个提交是 `76b39e92`。

## 1. 定位与分支

- **上游**：社区 LTS 分支 [FreeUnit](https://github.com/freeunitorg/freeunit)（Apache-2.0），remote 为 `freeunit`。
- **`master`**：纯上游镜像；每次同步后，本地 `master`、`origin/master` 和已抓取的 `freeunit/master` 必须指向同一个提交。自有补丁、维护文档和发布适配不进入此分支。
- **`codex/develop`**：日常开发和自有改动集成分支，保留原 master 的完整历史，并合入当前上游及自己的最新功能分支。
- **原 PR 分支**：`fu/php-unit-rewritten-path` 和 `codex/fix-request-target-spaces` 继续用于各自的上游 PR；同步主干时不重写它们。
- **历史备份**：2026-11-11 调整分支前的 master 为 `b07d8198`，已保存到远端 `codex/backup-master-20261111`。后续每次替换镜像 master 前也要备份其精确提交。

`codex/develop` 当前保留的自有功能补丁：

| 补丁 | 主要文件 | 最新自有提交 | 上游状态 |
|---|---|---|---|
| PHP `UNIT_REWRITTEN_PATH`，保留原始 `REQUEST_URI` 及 PHP filter 行为 | `src/nxt_php_sapi.c`、`test/test_php_application.py`、`test/php/query_string/index.php`、`test/php/rewritten_path/index.php` | `4110b29e`、`d07c7004` | [FreeUnit #829](https://github.com/freeunitorg/freeunit/pull/829)，等待审查；最初于 ndj888 1.36.0 发布 |
| 拒绝请求目标中的原始空格，保留 `%20` | `src/nxt_http_parse.c`、`src/test/nxt_http_parse_test.c`、`test/test_http_request_target.py`、`test/test_routing.py` | `6099d6bc`、`8a5e11d4` | [FreeUnit #970](https://github.com/freeunitorg/freeunit/pull/970)，等待审查 |

以下内容已经进入上游，不再作为活跃的分支差异保留：

- macOS 移植 `aae35c6f`（共享内存段不得短于 `PORT_MMAP_SIZE`）、`85a8d49c`（macOS C 测试与 CI）、`872bf041`（shm 映射计数）；develop 直接采用上游后续实现。
- 1.37.0 后的 Wasmtime、Tokio、rustls、webpki 依赖升级。历史贡献见 FreeUnit #825（`73d34155`，原 #815）及 #814；当前使用上游更新后的锁文件，Wasmtime 为 **48.0.8**，源码包与 audit 下载均使用上游镜像 `packages.freeunit.org`。

`codex/develop` 还要保留以下维护适配：

- README 的 fork 说明、SECURITY 顶部说明、本文件及 `docs/upstream/` 的历史投稿材料。
- `release-docker.yml`、`build-deb.yml`、`unitctl.yml`、`claude.yml` 的仓库防护，防止 fork 使用上游发布路径或凭据。
- `ndj888-release-assets.yml` 及其 checkout 提交、构建元数据和资产来源校验。
- `check-lock-audit.py` 的 `reviewed_pin` 校验及测试。当前 scope 绑定 Wasmtime 48.0.8 与上游 SHA512；保留上游 13 条 allow 及理由，不扩大例外。升级 pin 时先复核上游依据，再同步版本和摘要；同时保留上游更严格的 CMake flag 解析及 async feature 检查。

`master` 的纯镜像要求意味着它不包含上述自有 workflow 或仓库防护。fork 的 **`docker-nightly.yml` 必须在仓库 Actions 设置中保持 disabled**，同步上游后也要复核这一状态，避免默认分支定时发布镜像。其他发布操作必须显式核对目标仓库、ref 和权限。

此次调整只同步分支，不打 tag、不发布 Release、不部署生产。代码版本号随上游，不据此推断生产已经升级。

## 2. 节奏

| 频率 | 做什么 | 验收范围 |
|---|---|---|
| 每周 | 检查 FreeUnit 新提交、release 和安全公告，按第 3 节同步两条主干 | `master` 精确镜像；`codex/develop` 保留自有改动并验证 |
| FreeUnit 发布安全公告 | **72 小时内**评估、同步并完成相关回归；发布或部署另行授权 | 记录影响、测试与未完成的发布门槛 |
| 每次自有补丁更新 | 合入 `codex/develop`，查看对应上游 PR 状态 | 不把自有提交推入镜像 master，不重写其他 PR 分支 |
| 每次同步后 | 检查 fork Actions 状态，保持 `docker-nightly.yml` disabled | 默认分支上的上游 CI 不代表 develop 已测试 |
| 每季度 | 复查第 6 节待办；检查 FreeUnit 是否仍活跃 | 保留部署和安全历史记录 |

GitHub 的定时任务使用默认分支。当前 master 上的 audit/EOL 等检查只覆盖上游镜像；develop 中的补丁、`reviewed_pin` 和自有发布 workflow 需要在对应 ref 上单独验证，不能借用 master 的绿灯。

15 个验证 workflow 的 push 分支已加入 `codex/develop`；Clang AST 也接受以该分支为目标的 PR。原有路径过滤保留，因此是否运行仍取决于本次改动；`unitctl` 的开发分支触发只增加构建与测试覆盖，发布 guard、tag 和手动触发配置保持原样。空白和结构布局检查仅在事件基线为当前提交祖先时采用该基线；非快进同步遇到可达但非祖先的旧分支提交时，改用当前提交首父，避免把已修复的上游历史或旧 fork 的布局当成本次变化。历史 run 的红灯保留在原提交上，不会因后续修复自动变绿；应以修复后 develop 的精确提交和实际运行结果验收，旧提交的重试、未触发或被过滤的检查都不算新版本通过。

**上游失效判定**：FreeUnit 连续 90 天没有提交，或者一个高危公告 30 天内没有修复版本，就切换到第 5 节的“自主修复模式”；修复仍进入开发分支。

## 3. 同步 FreeUnit 与自有开发分支（当前流程）

先确认工作区干净、没有进行中的 merge，并记录上游及远端 master 的精确提交。所有 fetch/push 都指定 branch ref，不使用 `--mirror`、`--tags` 或推送 tag，也不 force-push 原 PR 分支。

下列命令用于本地和远端 `codex/develop` 都已建立后的日常同步。首次建立时，应先从旧 master 的备份创建开发分支，抓取上游及两个 topic 后完成 merge 和验收，再正常推送开发分支；此时省略尚不存在的远端 develop ref，且必须等开发历史安全保存后才替换镜像 master。本次的旧 master 基点是 `b07d8198`，备份名见第 1 节。

```bash
# remote 已配置；不拉取同名 release tag。
git config remote.freeunit.tagOpt --no-tags
git fetch --no-tags freeunit refs/heads/master:refs/remotes/freeunit/master
git fetch --no-tags origin refs/heads/master:refs/remotes/origin/master \
  refs/heads/codex/develop:refs/remotes/origin/codex/develop \
  refs/heads/fu/php-unit-rewritten-path:refs/remotes/origin/fu/php-unit-rewritten-path \
  refs/heads/codex/fix-request-target-spaces:refs/remotes/origin/codex/fix-request-target-spaces

# 保留双方历史；遇到冲突先处理并验收，再提交 merge。
git switch codex/develop
git merge --ff-only origin/codex/develop
git merge --no-ff freeunit/master
# 按需要合入已经审核的自有 topic 最新提交。
git merge --no-ff origin/fu/php-unit-rewritten-path
git merge --no-ff origin/codex/fix-request-target-spaces
# 完成下述验收后，正常推送开发分支。
git push origin refs/heads/codex/develop:refs/heads/codex/develop
```

冲突处理及验收：

1. 上游版本号、依赖、锁文件及修复采用较新的上游内容；源码差异应只剩第 1 节列出的两个功能补丁。
2. `CHANGES` 和 `docs/changes.xml` 跟随上游结构，把仍未合入的自有补丁条目放入当前未发布段；避免重复 backport 记录。
3. 保留 fork 说明、发布防护、provenance 与 `reviewed_pin`，不得用整文件旧版本覆盖上游新的安全检查。
4. 检查冲突标记、`git diff --check freeunit/master`、差异清单与提交祖先关系；确认原 master、自有 PHP/HTTP topic 及当前上游均可从 develop 追溯。
5. 在隔离的 Docker/Linux 环境编译并运行 C 测试、PHP 重写与 filter 回归、HTTP 原始空格与 `%20` 回归、审计脚本测试及适用的 CI。容器中完成验收，不在宿主机运行项目构建或运行时。审计失败要区分新增漏洞、既有上游例外与网络失败，不能宣称扫描为零。

开发分支安全推送并完成验收后，再镜像 master。以下命令要求本地 master 等于刚读取的远端 master；不一致时先调查并保存额外提交，不直接重置。master 若已在主 checkout 检出，应在那个 checkout 更新，不能用忽略其他 worktree 的选项抢占分支。

```bash
set -eu
# 在干净工作区执行；备份名每次唯一，不覆盖既有备份。
test -z "$(git status --porcelain)"
# 按实际环境设置已检出 master 的主 checkout 路径。
master_checkout=/Users/sword/GolandProjects/unit
test -z "$(git -C "$master_checkout" status --porcelain)"
test "$(git -C "$master_checkout" symbolic-ref --short HEAD)" = master
old_master=$(git rev-parse refs/remotes/origin/master)
test "$(git rev-parse refs/heads/master)" = "$old_master"
upstream_master=$(git rev-parse refs/remotes/freeunit/master)
backup_branch="codex/backup-master-$(date +%Y%m%d-%H%M%S)"
git push origin "$old_master:refs/heads/$backup_branch"
# 显式 lease：远端在准备期间变化时应失败，重新抓取与评估。
git push --force-with-lease="refs/heads/master:$old_master" \
  origin "$upstream_master:refs/heads/master"
git -C "$master_checkout" reset --hard "$upstream_master"
git fetch --no-tags origin refs/heads/master:refs/remotes/origin/master
test "$(git rev-parse master)" = "$(git rev-parse origin/master)"
test "$(git rev-parse master)" = "$(git rev-parse freeunit/master)"
# 当前集成 worktree 继续停留在 codex/develop。
```

推送后核对远端引用和 develop 的 CI 结果，并检查 fork 的 `docker-nightly.yml` 仍为 disabled。若远端或开发分支有并发变化，先重新评估，不能扩大 force-push 范围。

### 历史发布流程的适用范围

1.37.0 时曾使用 `release/X.Y.Z` 合并上游 release tag、向带有自有 workflow 的 master 提 PR、打同号 tag，并在 `release.published` 后上传资产。**这是历史流程，不适用于当前纯镜像 master**：默认分支没有 `ndj888-release-assets.yml`，不能假定发布 Release 就会自动生成或上传自有资产。未来发布前需另行设计并授权针对已验证开发 ref 的 workflow，核对 provenance、资产和 SHA256；此次不执行发布。

历史构建曾遇到沙箱 POSIX shm 限制、跨版本残留 `build/` 依赖、上游中间提交触发 whitespace lint，以及 PR 未触发 workflow。这些是排查线索，不可据此豁免当前失败检查；使用新容器构建并核对实际 CI 结果。

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

- [x] PHP `UNIT_REWRITTEN_PATH` 已提交 [FreeUnit #829](https://github.com/freeunitorg/freeunit/pull/829)（2026-10-26）。原分支 `fu/php-unit-rewritten-path`；develop 集成 `4110b29e` 和后续 `d07c7004` 的快速注册/filter 兼容修正，正文历史见 `docs/upstream/pr-php.md`。
- [ ] 跟进 #829 审查；合并后核对上游最终实现和回归测试，再删除 develop 的重复补丁差异。
- [x] HTTP 请求目标空格修复已提交 [FreeUnit #970](https://github.com/freeunitorg/freeunit/pull/970)；develop 集成 `6099d6bc`、`8a5e11d4`，原 PR 分支保持独立。
- [ ] 跟进 #970 审查；上游合并后核对 `%20` 与参数字符覆盖。
- [x] njs 1.0.2 建议已提交 FreeUnit #828（2026-10-26），当前上游已包含升级；正文历史见 `docs/upstream/issue-njs-1.0.2.md`。
- [x] 历史依赖更新已上游合并（#825、#814）；本次采用当前上游锁及 Wasmtime 48.0.8，恢复上游镜像地址，并更新 `reviewed_pin`。
- [x] 原“等待 1.37.1 同步”的三个 macOS backport 已随当前上游纳入，不再保留重复差异。
- [x] macOS 两个历史问题（ENOBUFS 卡住、zstd 并发超时）：2026-10-24 曾在 FreeUnit master `4beef78f` 上用 pytest 各跑 3 次通过；这是历史验收，不替代当前 develop 的验证。
- [ ] 继续复核 FreeUnit #343（未完成的分片消息占用文件描述符）的最新处理状态；1.37.0 曾把每个 port 的上限设为 64 个。
- [x] 复核 nginx/unit 遗留问题的历史结果：#1626 已由 FreeUnit #540 跟踪；#1653 当时未复现；#1370 在 FreeUnit 中已有 RHEL 8/9 的 Python 3.12 RPM 配方。
- [ ] 向上游投稿前核对当前凭据权限。历史上 fine-grained token 对非自己仓库返回过 403；不能把该历史错误视为所有 token 的固定限制。
- [ ] 名称与商标：“NGINX”是 F5 的商标。未来 Release 标题可使用“Unit (ndj888 build)”等名称，避免看起来像官方发布。
- [ ] 如需发布 fork Docker 镜像，单独设计推送至自有命名空间的流程并明确授权。develop 保留上游发布仓库防护；master 为纯镜像，fork 仓库层必须保持 `docker-nightly.yml` disabled。
- [ ] 未来发布前重新设计自有 Release 资产触发方式：当前默认 master 不包含自有资产 workflow，不能沿用历史自动上传假设。
- [ ] 生产镜像曾无法从零重建：us-df-v2 上的 `nc-unit:php-8.5.12-unit-1.37.0-20261024` 以本地镜像 `nc-unit:php-8.5.12-security-20261021` 为基础（`nc-docker/unit-1.37.0/Dockerfile.unit-1.37.0`），后者原构建目录已丢失。下次生产升级前需复核现状，并补一份从 `php:8.5` 官方镜像开始的完整 Dockerfile。
- [ ] 定期查看默认 master 的 audit/EOL 结果；为 develop 的自有补丁与精确 pin 检查保留独立 CI 证据，不能把镜像分支结果当作开发分支验收。

## 6.1 生产部署记录

- 2026-10-24：us-df-v2 上的 Nextcloud app 容器从 Unit 1.35.0 切换到 1.37.0（ndj888 构建 efd3dbe2，PHP 8.5.12）。新镜像只替换了 `unitd` 和 `php.unit.so`，编译参数与原镜像相同。切换前先用测试容器确认 1.37.0 能导入 `unit-config/nextcloud-unit.json`；切换时只重建 app 容器（`docker compose up -d --no-deps app`），中断约 30 秒。
- 回滚：`cd ~/docker/nextcloud && cp -p docker-compose.yaml.bak-unit137-20261024 docker-compose.yaml && docker compose up -d --no-deps app`。旧镜像另有 tag `nc-unit:php-8.5.12-rollback-20261024`。
- 注意：Unit 的状态目录没有挂载卷，每次重建容器都会重新导入 `nextcloud-unit.json`，所以升级 Unit 之前一定要先用测试容器验证这份配置能被新版本接受。服务器上没有 buildx，构建时要设置 `DOCKER_BUILDKIT=0`。

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
