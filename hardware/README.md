# Hardware

[← simdash](../README.md)

Physical parts for the rig: CAD sources, files ready to print, photos. One folder per project, each with
its own README.

| Folder | What |
|---|---|
| [shaker-mounts](shaker-mounts/README.md) | 3D-printed mounts that hold the bass shakers on the rig (FreeCAD). |

The [Desk 6DOF](../docs/6dof/README.md) parts (base, platform, servo brackets, car mount) will go in
`hardware/6dof/` the same way once they are designed.

## Conventions

- `freecad/`: the sources (`.FCStd`). They are what to edit; keep them in step with the exports.
- `print/`: exports ready to slice (`.3mf`), named exactly like their source
  (`freecad/rear-puck-mount.FCStd` → `print/rear-puck-mount.3mf`).
- `img/`: photos and renders, referenced from the folder's README. A part's render is named like the part
  (`img/rear-puck-mount.png`); photos of the rig get a descriptive name (`img/rig-front-left.jpg`).
- Names: English, lower case, words joined by hyphens, *where* then *what* (`seat-bst1-mount`,
  `front-puck-mount`). No version suffixes (`2`, `_v3`): git keeps the history.
- Lengths in mm. Write down in the README what each part fits (tube diameter, shaker model, screw size), as
  that is what nobody remembers six months later.
- FreeCAD's backup files (`*.FCBak`, `*.FCStd1`) are ignored by git.
