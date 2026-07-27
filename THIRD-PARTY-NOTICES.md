# Third-party notices

Tenantless is licensed under [Apache-2.0](LICENSE). It redistributes the
third-party components listed below under their own licenses.

## Fonts

The web console ships two font families as `.woff2` subsets under
`frontend/public/fonts/`. Both are licensed under the **SIL Open Font License,
Version 1.1**, whose terms require that each copy carry the copyright notice and
the license. The full, verbatim upstream license text for each family is
included next to the font files and is served alongside them.

### DM Sans

```
Copyright 2014 The DM Sans Project Authors (https://github.com/googlefonts/dm-fonts)
```

- **License:** SIL Open Font License 1.1 — verbatim text at
  [`frontend/public/fonts/DMSans-OFL.txt`](frontend/public/fonts/DMSans-OFL.txt)
- **Upstream:** <https://github.com/googlefonts/dm-fonts> (`Sans/OFL.txt`)
- **Shipped file:** `frontend/public/fonts/dmsans-latin.woff2`
- **Modification:** the upstream font was subset to the Latin range and converted
  to WOFF2. The font is redistributed under its original name, unmodified in
  design, so no Reserved Font Name applies.

### Space Mono

```
Copyright 2016 The Space Mono Project Authors (https://github.com/googlefonts/spacemono)
```

- **License:** SIL Open Font License 1.1 — verbatim text at
  [`frontend/public/fonts/SpaceMono-OFL.txt`](frontend/public/fonts/SpaceMono-OFL.txt)
- **Upstream:** <https://github.com/googlefonts/spacemono> (`OFL.txt`)
- **Shipped files:** `frontend/public/fonts/spacemono-400-latin.woff2`,
  `frontend/public/fonts/spacemono-700-latin.woff2`
- **Modification:** subset to the Latin range and converted to WOFF2, at weights
  400 and 700. Redistributed under its original name, unmodified in design, so no
  Reserved Font Name applies.

Both notices and license texts were retrieved verbatim from the upstream Google
Fonts repositories rather than transcribed, so they match the sources exactly.

## Runtime dependencies

Python, Rust and npm dependencies are declared in `pyproject.toml`,
`mock-server/Cargo.toml` and `frontend/package.json`, and pinned in `uv.lock`,
`Cargo.lock` and `frontend/package-lock.json`. Each carries its own license; none
is vendored into this repository.

## Trademarks

Azure and Microsoft are trademarks of Microsoft Corporation. Tenantless is an
independent project and is not affiliated with, endorsed by, or sponsored by
Microsoft. Microsoft ARM resource-type names and API shapes are referenced
descriptively, to describe the API surface this project emulates.
