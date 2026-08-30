# Acceptance: verified-blocker swap map rescue

- Run: 2026-08-30T17:31:12  |  model: `glm-5.2`
- API calls: 10 (cap 60)

## Map
- existing: 62
- + verified_single -> [pol-term]: 18/24
- + isolated phrases -> [pol-phrase]: 0/2
- dupes skipped: 8
- merged total: 80 (pre-round-2); final: 80

## Hard threads
- id=136080: c0=BLK c1=pass swapped=1 verdict=rescued-round-1
- id=138356: c0=BLK c1=pass swapped=1 verdict=rescued-round-1

## Spot checks (recent storied pol, swapped)
- id=145491: pass (swapped=0)
- id=145490: pass (swapped=0)
- id=145489: pass (swapped=1)
- id=145488: pass (swapped=0)
- id=145487: pass (swapped=0)

## Result: SUCCESS — rescued 2/2, spot 5/5

Deployed: /home/fivelidz/projects/MASTER_PROJECTS/chanalyse/ops/filter_vocab/swap_map.json (80 terms; archived prior as swap_map_20260830_173152.json)

Residual triggers (count only): 0
