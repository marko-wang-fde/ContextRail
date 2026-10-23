# ContextRail schema

Read this reference when creating, changing, migrating, or repairing managed Markdown. Ordinary navigation does not require it.

## Configuration

Place `.context-rail.json` at the repository root:

```json
{
  "version": 1,
  "roots": ["workspace/projects"],
  "entrypoints": ["workspace/projects/_index.md"],
  "exclude": ["**/.git/**", "**/node_modules/**"],
  "max_depth": 12
}
```

- `roots` contains repository-relative directories scanned for managed Markdown.
- `entrypoints` contains repository-relative Markdown files allowed to omit an upstream.
- `exclude` contains repository-relative glob patterns.
- `max_depth` is the recommended maximum downstream depth from an entrypoint.

All paths use `/`, are relative to the repository root, and may not contain `..`.

## Loading-chain block

Use exactly one marked block per managed file:

```markdown
## 加载链

<!-- context-rail:start -->
**上游**
- `workspace/projects/_index.md §项目目录` — 当任务涉及项目 A 时进入本文件

**按需下游**
- `workspace/projects/project-a/requirements.md §核心需求` — 分析或修改需求时读取
- `workspace/projects/project-a/architecture.md §系统边界` — 讨论技术方案时读取

**同步对象**
- `workspace/projects/project-a/api.md §接口列表` — 数据模型发生变化时同步
<!-- context-rail:end -->
```

Entrypoints may omit `上游`. Leaf documents may omit `按需下游` and `同步对象`. Omit an unused relation section rather than adding placeholders such as “无”.

## Pointer grammar

Each pointer is one Markdown bullet:

```text
- `<repository-relative-path> §<exact-heading-text>` — <when to follow or synchronize>
```

Rules:

- The target file must exist under a configured root and contain a ContextRail block.
- The heading text after `§` must exactly match one Markdown heading in the target file and be unique there.
- The reason must describe a concrete task or change condition; do not write “相关时”“需要时” or “参见文件”.
- `上游` means the parent that makes this document discoverable.
- `按需下游` means a child that may be read when its reason matches the current task.
- `同步对象` means a file that may require a coordinated write. It does not authorize default reading or writing.

## Bidirectional invariant

If parent `A` lists child `B` under `按需下游`, then `B` lists `A` under `上游`. The headings differ because each pointer identifies the precise landing section in its target:

```text
A --按需下游--> B §目标内容
B --上游------> A §路由入口
```

Synchronization pointers may be directional and do not require a reciprocal pointer.

## Authority and hierarchy

- Each fact has one authoritative document.
- Index files route; overview files summarize state and route within one project; leaf files contain detailed content.
- A child adds detail instead of copying rules or facts from its parent.
- Cross-project links should normally live in an index. Avoid dense peer-to-peer links among archived projects.
- Split large archives into intermediate indexes by year, client, or domain before a single index becomes difficult to scan.

## Change protocol

### Create

Create the document and its block, add the parent's downstream pointer, then validate both files.

### Move or rename

Search for the old repository-relative path, update every pointer in one change, verify the old path has zero remaining hits, then run strict validation.

When the old file may still exist as a copy or redirect, make the rename explicit so validation can distinguish it from a legitimate path:

```bash
./context-rail check --strict --rename old/path.md=new/path.md
```

### Delete

Find all inbound pointers first. Remove or redirect them only when the intended successor is known. Never silently leave a missing target.

### Migrate legacy Markdown

Audit first. Add markers and explicit pointers only to files selected for management. Do not bulk-infer relationships from directory proximity, filenames, or modification times.
