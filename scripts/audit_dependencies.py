"""Consulta OSV somente com nomes/versões do inventário explicitamente indicado."""

import argparse
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--osv", action="store_true", required=True,
                        help="Autoriza enviar apenas nomes e versões à API pública OSV")
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    packages = inventory["packages"]
    if not 1 <= len(packages) <= 200:
        parser.error("Inventário deve conter de 1 a 200 pacotes")
    queries = []
    for package in packages:
        name, version = package["name"], package["version"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name):
            parser.error("Nome de pacote inválido")
        if not re.fullmatch(r"[A-Za-z0-9_.+!-]{1,100}", version):
            parser.error("Versão de pacote inválida")
        queries.append({"package": {"name": name, "ecosystem": "PyPI"}, "version": version})
    request = Request("https://api.osv.dev/v1/querybatch",
                      data=json.dumps({"queries": queries}).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    # URL fixa; nenhum conteúdo do projeto, variável de ambiente ou chave no payload.
    with urlopen(request, timeout=45) as response:
        results = json.load(response)["results"]
    if len(results) != len(packages):
        raise RuntimeError("OSV retornou um resultado incompleto")
    findings = [{**package, "advisories": [v["id"] for v in result.get("vulns", [])]}
                for package, result in zip(packages, results) if result.get("vulns")]
    report = {"source": "OSV", "environment": inventory.get("environment"),
              "checked_packages": len(packages), "matched_results": len(results),
              "complete": True, "findings": findings}
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"checked_packages": len(packages), "findings": findings}))
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
