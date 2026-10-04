# Neutral skill sources

Skills are **compiled, not copied**.

A skill bundles two things with different portability. **Instruction content** - the prose describing how the work is done - ports almost perfectly. **Plumbing** - frontmatter schema, invocation metadata, tool names, script runners, file layout - does not port at all.

So we author neutral sources here: instruction body plus a manifest declaring which HAL capabilities the skill consumes, and which archetypes and industries the variant serves. At provisioning, the adapter compiles each source into a harness-native package.

Swapping harness means writing a second compiler backend, not rewriting the library.

## The inverse matters

Harnesses mutate skills - same-turn repair, auto-learning, scheduled review. Without a return path the neutral source silently becomes fiction.

Compiled artifacts carry generated-region markers. `skill.lift` operates only on authored regions. A patch touching a generated region is rejected and alerted: it means either a compiler defect or a skill rewriting its own plumbing.

**Never sync files. Sync diffs, through review.**

## Ownership

| Owner | Auto-apply | Learning mode |
|---|---|---|
| Vendor (core, council, domain library) | Never | propose |
| Adapted (vendored community) | Never | propose |
| Tenant (learned in one venture) | Yes | auto |
