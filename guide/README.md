# guide

`te-guide.css` styles a lab's instruction pages in the Instruqt player. It is not in the image; it
reaches a page through jsDelivr, so it serves any lab whether or not the lab pins te-runner.

```html
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/wiz-training/te-runner@<sha>/guide/te-guide.css">
```

| Constraint | Why |
|---|---|
| Pin a commit sha, never `@main` | a sha URL is immutable; `@main` is cached by jsDelivr for hours, so a change lands at no predictable time |
| The host is `cdn.jsdelivr.net` | the player's CSP `style-src` admits `'self' 'unsafe-inline' *.instruqt.com *.instruqt.dev fonts.googleapis.com cdn.jsdelivr.net cdn.announcekit.app`; any other host is refused |
| Every selector starts `.prose-lab-agent-page` | a page's stylesheet applies to the whole player document while that page is mounted |
| Colours are `--lab-agent-*` / `--tw-prose-*` theme tokens | a lab on another theme inherits them instead of a hard-coded dark palette |

A stylesheet that fails to load leaves the player's default rendering; nothing on the page breaks.
Reproducer: te-lab-md-test page 3 (player default) against page 4 (this sheet).

Open: whether the lab editor's Visual mode keeps a `<link>` line on save.
