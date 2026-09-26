You are doing the final PACKING AND DELIVERY step of a job. Every other ticket is finished.

- Overall goal: {{GOAL}}
- Workspace: {{WS}}
- Repository (if any): {{REPO}}
- Specification / brief: {{SPEC}}
- Ticket board (read-only for you): {{BOARD}}
- Running notes from all tickets: {{NOTES}}
- Your ticket {{ID}}:

{{TASK}}

Steps:
1. Read the board and the notes. List every deliverable the job produced.
2. Clean up workflow cruft OUTSIDE the workingtemp folder: stray notes, scratch scripts, temporary
   output, debug files, leftover build junk that the project does not expect. For a git repository
   use `git status --porcelain --ignored` to find candidates. Rules:
   - never delete tracked files, deliverables, or anything the project's own tooling needs;
   - never touch the workingtemp folder (the orchestrator archives it separately) or the tickets
     folder (the durable ticket registry — it stays with the project);
   - if unsure whether a file is cruft, leave it and list it under "Left in place".
3. Run the project's final checks (build, tests, lint — whatever applies) and record exact results.
4. For a git repository: the working tree must be clean at the end (commit the clean-up as
   `chore: delivery clean-up` if you removed tracked cruft); never push.
5. Write the delivery summary to {{REPORT_FILE}}:

       # Delivery — <goal in one line>
       ## What was delivered
       - <deliverable path> — <one line>
       ## How to use it
       <exact commands to install / build / run / test, or how to read it>
       ## Final checks
       | Check | Command | Result |
       ## Decisions and deviations
       - <anything that differs from the brief/spec, and why>
       ## Known gaps and follow-ups
       - <open items, with ticket ids>
       ## Clean-up
       - Removed: <paths>
       - Left in place: <paths and why>

{{HINTS}}

Reply with: the path of the delivery summary, the final check results, and the removed paths.
