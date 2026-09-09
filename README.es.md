<!-- synced-from: 40e1a1dcf67305bafec982835cf8676b23c8b1a6 -->
# webhook-replay

**Español** · [English](README.md)

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)

**Captura un webhook una vez y dispáralo contra tu aplicación local las veces que necesites — sin volver a provocar el evento real en el proveedor.**

Depurar una integración de webhooks suele significar volver a Stripe / GitHub / Shopify y reenviar el evento a mano cada vez que tocas tu manejador. `webhook-replay` rompe ese bucle: levantas un endpoint local que graba cada petición entrante (cabeceras, cuerpo, marca de tiempo) y luego reproduces la que quieras contra tu aplicación cuando te convenga — con filtros, exportación a `curl` y comparación de cargas por el camino.

Cero dependencias en ejecución. Sólo biblioteca estándar de Python.

> **Nota sobre el idioma.** Este README está en las dos lenguas. El resto de la documentación y los comentarios del código están en inglés.

---

## Qué trae

- **Captura todo** — acepta cualquier método en cualquier ruta, y guarda cabeceras completas, cuerpo en crudo, dirección de origen y marca de tiempo en un fichero SQLite local.
- **Reproduce contra tu aplicación** — reenvía una petición capturada (o las últimas _N_, o la misma _N_ veces) a la URL base que quieras. Las respuestas que no sean 2xx se informan, no se tragan.
- **CLI legible** — `list` / `show` con color y cuerpos JSON formateados, o `--json` para automatizar.
- **Exportación a `curl`** — convierte cualquier petición capturada en un comando que puedes pegar.
- **Comparación de cargas** — compara dos cuerpos capturados; el JSON se canonicaliza antes, así que el ruido de orden de claves desaparece y sólo se ven los cambios reales.
- **Filtros** — por método, por trozo de ruta o por antigüedad (`--since 10m`).
- **Secretos enmascarados en la salida** — `Authorization`, `Cookie`, firmas y cabeceras de clave de API salen como `<redacted>` en `show`, `list --json` y la exportación a `curl`, para que un comando pegado sea seguro. `--show-secrets` lo desactiva; la reproducción siempre envía los valores reales.
- **Acotado por defecto** — tope de tamaño de cuerpo (`413` por encima), tope de retención que descarta las capturas más viejas, y tiempo de espera de lectura, para que un emisor descarriado no te llene el disco ni te clave un hilo.
- **Nada que instalar salvo Python** — sin framework, sin broker, sin servicio externo.

---

## Instalación

```bash
pip install webhook-replay
```

Necesita Python 3.9+. También puedes ejecutarlo desde un clon sin instalar: `python -m webhook_replay --help`.

---

## Uso

### 1. Arranca el servidor de captura

```console
$ webhook-replay serve --port 8973
webhook-replay listening on http://127.0.0.1:8973
  storing captures in /home/tu/.webhook-replay/captures.db
  limits: body 1048576 bytes, retain 1000 newest, read timeout 30s
22:42:34 #1 POST /github/webhook (34 bytes)
```

Apunta el webhook de tu proveedor (o un túnel tipo ngrok/cloudflared) a ese endpoint. Cada petición se registra en vivo y se persiste.

### 2. Mira lo que ha entrado

```console
$ webhook-replay list --method POST --path stripe --since 10m
```

Añade `--json` para llevártelo a otro sitio.

### 3. Inspecciona una petición

```console
$ webhook-replay show 1
POST /stripe/webhook?livemode=false
#1  2026-08-21T22:41:43.423+00:00  from 127.0.0.1

Headers
  Content-Type: application/json
  Stripe-Signature: <redacted>
  (algunos valores van enmascarados; usa --show-secrets para verlos)
```

### 4. Reprodúcela contra tu aplicación

```console
$ webhook-replay replay 1 --to http://127.0.0.1:8972 --show-response
#1 POST http://127.0.0.1:8972/stripe/webhook?livemode=false -> 200 OK 3ms
```

Tu manejador local recibe una copia byte a byte de la petición original. Puedes reproducir las dos más recientes dos veces cada una en un solo comando (`--last 2 --times 2`), cambiar el método (`--method POST`) o inyectar cabeceras (`--header 'X-Debug: 1'`).

### 5. Expórtala como curl

```console
$ webhook-replay curl 2 --base https://api.miapp.local
curl -X POST 'https://api.miapp.local/stripe/webhook?livemode=false' \
  -H 'Content-Type: application/json' \
  -H 'Stripe-Signature: <redacted>' \
  --data-binary '{"id": "evt_2", "type": "invoice.paid"}'
```

Las cabeceras de firma, `Authorization`, `Cookie` y claves de API van enmascaradas para que el comando se pueda pegar en una incidencia o en un chat sin susto.

### 6. Compara dos cargas

```console
$ webhook-replay diff 1 2
```

Los dos cuerpos se canonicalizan como JSON con las claves ordenadas antes de comparar, así que reordenar claves no aparece como cambio — sólo las diferencias de valor reales.

---

## Referencia de comandos

| Comando | Qué hace |
| --- | --- |
| `serve` | Arranca el endpoint local de captura (`--host`, `--port`, `--status`, `--response`, `--max-body`, `--max-captures`, `--read-timeout`). |
| `list` | Lista las peticiones capturadas (`--method`, `--path`, `--since`, `--limit`, `--json`, `--show-secrets`). |
| `show <id>` | Muestra una petición entera (`--raw` escribe sólo el cuerpo, `--show-secrets` desenmascara). |
| `replay <id...>` | Reproduce petición(es) contra `--to <url>` (`--last N`, `--times N`, `--method`, `--header`, `--show-response`). |
| `curl <id>` | Imprime una petición como comando `curl` (`--base <url>`, `--show-secrets`). |
| `diff <a> <b>` | Compara dos cuerpos capturados. |
| `prune` | Borra capturas viejas (`--older-than 7d`, `--keep N`). |
| `clear` | Borra todas las capturas (`-y` para saltarse la confirmación). |

Globales: `--db <ruta>` para usar otro almacén, `--no-color` para desactivar el color (también respeta `NO_COLOR`).

---

## Seguridad

**El almacén de capturas guarda credenciales reales en texto claro.** Cada petición se guarda tal y como llegó —cabeceras completas y cuerpo completo— en un fichero SQLite local (`~/.webhook-replay/captures.db` por defecto). El tráfico de webhooks lleva de forma rutinaria cabeceras `Authorization`, cookies de sesión, firmas y claves de API, y todo eso acaba en ese fichero sin cifrar. **Es deliberado**: una reproducción sólo es fiel, y una firma sólo verifica, si los bytes guardados son los originales.

Como el almacén no se sanea, lo que se sanea es la **salida**:

- `show`, `list --json` y `curl` enmascaran los valores de `Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie` y cualquier cabecera cuyo nombre contenga `secret`, `token`, `signature`, `hmac`, `api-key`/`api_key` o un segmento `sig`. Salen como `<redacted>`, conservando el prefijo del esquema cuando lo hay (`Bearer <redacted>`).
- `--show-secrets` desactiva el enmascarado para un comando concreto.
- **La reproducción nunca se enmascara.** Reenvía las cabeceras capturadas literalmente, que es justo para lo que existe la herramienta.
- El enmascarado va por nombre, no por valor, y sólo cubre las vías de salida. Es una defensa contra pegar un secreto en una incidencia o en una pantalla compartida — **no una garantía** de que ningún secreto pueda aparecer en ningún sitio.

Sobre el almacén:

- **No lo subas al repositorio y no lo compartas.** El `.gitignore` ya excluye `*.db`, `*.sqlite3` y `captures.db`, pero tenerlo fuera del repo es más seguro todavía.
- **Bórralo al terminar.** `webhook-replay clear -y` lo vacía; `prune --older-than 7d` tira lo de más de una semana; `rm ~/.webhook-replay/captures.db` se lo lleva entero.
- **Mantén el servidor en local.** Escucha en `127.0.0.1` por defecto y responde a *cualquier* método en *cualquier* ruta con un éxito enlatado. Exponerlo (`--host 0.0.0.0`, o dejar un túnel abierto) lo convierte en un sumidero abierto y sin autenticación para lo que haya en la red.
- **Los valores por defecto están acotados, no a cero.** `serve` rechaza cuerpos de más de 1 MiB con `413`, retiene las 1000 capturas más nuevas y corta una conexión que se queda parada 30 segundos.

Para reportar una vulnerabilidad, ver [SECURITY.md](SECURITY.md).

---

## Cómo funciona

- **Captura** — un manejador de `http.server` con hilos registrado para todos los métodos. Lee el cuerpo (`Content-Length` bytes, o un flujo `Transfer-Encoding: chunked` decodificado al vuelo), fotografía las cabeceras en orden y escribe una fila en SQLite. Una petición que traiga a la vez `Content-Length` y `Transfer-Encoding`, o una longitud mal formada, se rechaza con `400` en vez de adivinar. Responde con una respuesta enlatada configurable (por defecto `200 {"received": true}`) más una cabecera `X-Webhook-Replay-Id`. Cada respuesta cierra la conexión, así que una conexión sirve exactamente una petición.
- **Almacenamiento** — una tabla SQLite y una conexión corta por operación (con tiempo de espera de bloqueo), lo que la hace segura bajo los hilos por petición del servidor. Los cuerpos se guardan como `BLOB`, así que las cargas binarias van y vuelven exactas.
- **Reproducción** — el método, la ruta (con la cadena de consulta) y el cuerpo en crudo se reconstruyen en una petición de `urllib` contra tu URL base. Las cabeceras salto a salto que describen la conexión *original* (`Host`, `Content-Length`, `Connection`, `Accept-Encoding`) se descartan y se recalculan; todo lo demás —firmas incluidas— se reenvía literal, con una limitación: los nombres de cabecera repetidos se colapsan al último valor, porque `urllib` guarda un valor por nombre.
- **Enmascarado** — vive en un módulo y se ejecuta sólo donde una captura se *presenta*: exportación a `curl`, `list --json` y `show`. Nada filtra la vía de captura ni la de reproducción, así que lo que se guarda y lo que se reenvía siguen siendo los bytes originales.

Todo es biblioteca estándar: `http.server`, `sqlite3`, `urllib`, `argparse`, `difflib`, `json`, `shlex`.

---

## Desarrollo

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

La batería (155 pruebas) es autocontenida: levanta servidores reales de captura y de recepción en puertos libres que asigna el sistema, y ejercita la captura (incluidos cuerpos troceados y tramas mal formadas), la persistencia, escrituras concurrentes, los límites de tamaño / retención / tiempo, la reproducción (éxito, no-2xx, error de conexión, destino atascado, cambios de método y cabecera, reenvío literal de cabeceras sensibles), todos los comandos del CLI, el enmascarado de salida, la exportación a `curl` y la comparación de JSON de punta a punta. Sin red ni servicios externos.

---

## Parte de una familia de herramientas pequeñas

`webhook-replay` es una de una familia de herramientas pequeñas y con pocas dependencias que construyo y mantengo en abierto — cada una una utilidad concreta pensada para hacer bien un trabajo:

- [The GEO Handbook](https://github.com/ferinazumaDEV/generative-engine-optimization-handbook) — la referencia abierta sobre conseguir que los motores de respuesta de IA te citen.
- [politeclient](https://github.com/ferinazumaDEV/politeclient) — un cliente HTTP cuidadoso y bien educado para Python: reintentos con espera, límite de ritmo por host, caché y paginación.
- [scaffld](https://github.com/ferinazumaDEV/scaffld) — genera proyectos Python completos (pruebas, CI, pre-commit, licencia) desde plantillas, con interfaz de terminal.
- [typedout](https://github.com/ferinazumaDEV/typedout) — salida estructurada fiable de OpenAI y Anthropic, con interfaz de proveedor para añadir otros.
- Web y publicaciones: [zentimes.es](https://zentimes.es).

De [ferinazumaDEV](https://github.com/ferinazumaDEV).

---

## Licencia

MIT — ver [LICENSE](LICENSE).

---

_Hecho por Fernando Aporta Franco ([@ferinazumaDEV](https://github.com/ferinazumaDEV))._
