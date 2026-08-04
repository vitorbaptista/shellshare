# Reading a shellshare broadcast as an agent

The reader that used to live here as two scripts is now one file,
[`templates/agent.mjs`](../templates/agent.mjs):

```bash
node agent.mjs '<url>'            # the history so far, then exit
node agent.mjs '<url>' --follow   # history, then live (needs Node >= 22)

timeout 60 node agent.mjs '<url>' --follow          # bound the wait
node agent.mjs '<url>' --follow | grep -m1 'DONE'   # wait for a marker
```

You do not need this repo to get it. It is inlined into
<https://shellshare.net/llms.txt> and into every room page, so whichever
of those you fetched already has it. The room page's copy sits in a
`<pre>`, so anything that parses the HTML decodes it for you;
`/llms.txt` carries it verbatim either way.

Full agent-facing docs: [AGENTS.md](../AGENTS.md).
