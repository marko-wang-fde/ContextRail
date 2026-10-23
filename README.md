# ContextRail

**沿链加载，只读所需。**

ContextRail 是一个项目级 Codex Skill，用显式 Markdown 指针组织大量项目文件。它让 Codex 从项目索引进入 `_overview.md`，再只沿当前任务需要的路径读取内容。

ContextRail 不读取聊天记录，不维护人格或长期记忆，不安装 hooks、子代理、定时任务，也不包含任何“做梦”功能。

## 包含内容

- `AGENTS.md`：极薄的项目路由规则。
- `.agents/skills/context-rail/`：Codex Skill、格式规范和确定性校验器。
- `.context-rail.json`：扫描根目录和入口文件配置。
- `workspace/projects/_index.md`：空白项目索引。
- `context-rail`：零依赖命令行入口。

## 快速开始

把本目录作为新仓库使用时：

```bash
./context-rail check
./context-rail graph
```

安装到已有仓库时，在 ContextRail 包目录执行：

```bash
./context-rail init /absolute/path/to/repository
```

初始化遵循“不覆盖”原则：只创建缺失文件。如果目标仓库已有 `AGENTS.md`，命令不会修改它，而会输出一段需要人工合并的路由规则。已有的 `context-rail` 启动脚本或入口索引若与本工具冲突，初始化会明确失败，不会报告虚假的成功状态。

初始化完成后，在目标仓库中运行：

```bash
./context-rail check
./context-rail check --strict
./context-rail check --rename old/path.md=new/path.md
./context-rail graph
./context-rail graph --format json
```

## 工作方式

```text
AGENTS.md
  -> workspace/projects/_index.md
    -> workspace/projects/<project>/_overview.md
      -> 当前任务真正需要的文件
```

每个受管 Markdown 文件使用一个带边界标记的加载链区块。完整格式见 `.agents/skills/context-rail/references/schema.md`。

## 命令

- `init [ROOT]`：把 Skill、配置、入口索引和命令行入口安装到仓库；不覆盖已有文件。
- `check [--strict] [--rename OLD=NEW] [--format text|json]`：检查断链、失效锚点、缺少回指、孤立文件、循环和重命名后的旧路径残留。
- `graph [--format text|json|dot]`：显示从入口文件可达的加载链。

退出码：`0` 表示没有错误；`1` 表示校验错误；`2` 表示参数、配置或运行环境错误；`3` 表示未预期的内部错误。普通模式下未纳管文件是警告；`--strict` 会把警告视为失败。

## 大型归档建议

- 活跃项目与归档项目使用不同的入口索引。
- 项目数量较多时按年份、客户或业务域增加中间索引，不要制造一个巨型索引文件。
- 归档项目尽量只读；跨项目关系放到索引，不建立密集的同级联动网。
- 精确项目导航用 ContextRail；跨全库全文检索继续使用 `rg` 或结构化清单。

## 安全边界

- 读取和审计默认不修改文件。
- 校验器不猜测缺失关系，也不自动“修复”语义指针。
- 校验器证明的是结构完整性，不会判断两个项目在业务语义上是否应该连接；需要隔离保证时同时审查 `graph` 输出。
- 移动、重命名或删除受管文件时，应由 `$context-rail` 同步修改受影响指针，然后运行 `check`。
- 如果入口或项目无法确定，停止并询问，不扫描 Codex 会话推断。
