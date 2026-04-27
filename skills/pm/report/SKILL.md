---
name: report
description: >
  Append a TaskReport (proof of work / progress note) to a task. Reports are
  immutable and chained: each new report links prevReport to the previous
  one. A report is required before planning:finished will accept the task.
  Use when an agent finishes its work and produces output, or when capturing
  partial progress mid-execution.
---

# planning:report — submit a report on a task

## Procedure

```
../scripts/pm report --task <task-sha> --title "..." \
    (--text "..." | --text-file path/to/report.md)
```

- Appends a TaskReport with `links.task = <task-sha>` and
  `links.prevReport = <previous-report-sha>` (omitted on the first report).
- The report body is the proof of work: include outputs, file paths,
  diffs, test results — whatever evidence justifies completion.
- Multiple reports per task are allowed (e.g. interim updates). The last
  report is the one that `planning:finished` will reference as `proof`.
