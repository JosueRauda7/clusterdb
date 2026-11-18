#!/opt/patroni/bin/python
"""Renderiza la plantilla de Patroni y decide si este nodo puede bootstrappear.

Solo postgres-master tiene PATRONI_BOOTSTRAP_ENABLED=true. Así se evita que la
réplica gane la primera elección y que los nombres Master/HA queden invertidos
en el primer arranque.
"""
import os
import sys
from string import Template

import yaml


def main() -> int:
    if len(sys.argv) != 3:
        print("uso: render-patroni-config <plantilla> <salida>", file=sys.stderr)
        return 2

    template_path, output_path = sys.argv[1], sys.argv[2]
    with open(template_path, "r", encoding="utf-8") as fh:
        raw = fh.read()

    try:
        rendered = Template(raw).substitute(os.environ)
    except KeyError as exc:
        print(f"[clusterdb] Variable de entorno faltante: {exc}", file=sys.stderr)
        return 3

    config = yaml.safe_load(rendered)
    bootstrap_enabled = os.getenv("PATRONI_BOOTSTRAP_ENABLED", "false").lower() == "true"
    if not bootstrap_enabled:
        config.pop("bootstrap", None)

    with open(output_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(config, fh, sort_keys=False, default_flow_style=False,
                       allow_unicode=True)

    print(f"[clusterdb] Configuración renderizada en {output_path} "
          f"(bootstrap={bootstrap_enabled})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
