# video-editor-service (Jotarondon8)

Micro-servicio HTTP con FFmpeg que recibe un video + un PNG transparente (pie de
página de marca, renderizado por n8n con Gotenberg) y devuelve un Reel vertical
9:16 listo para publicar: recorte, color cinematográfico, zoom lento (Ken
Burns), fade in/out, subtítulos automáticos (Whisper local) y música de fondo
opcional. Disponible en una URL pública temporal (expira sola a los 30 minutos).

No contiene ningún token ni credencial: todo llega por variables de entorno.

## Endpoints

- `GET /health` — sin auth, para el healthcheck de EasyPanel.
- `POST /reel` — requiere header `X-API-Key`. Form-data:
  - `file` (video, requerido)
  - `overlay` (PNG transparente, opcional)
  - `start` (`HH:MM:SS`, default `00:00:00`)
  - `duration` (segundos, 1-180, default `60`)
  - `music_url` (enlace directo a un .mp3/.wav, opcional — se mezcla bajando el audio original al 25%)
  - Responde `{"job_id": "...", "url": "https://.../files/<job_id>.mp4"}`
- `GET /files/{job_id}.mp4` — público a propósito (Instagram debe poder
  descargarlo sin headers). El `job_id` es un UUID impredecible y el archivo se
  autoborra a los 30 min.
- `DELETE /files/{job_id}` — requiere `X-API-Key`, borrado manual inmediato.

## Variables de entorno (se configuran en EasyPanel, nunca en este repo)

- `API_KEY` — clave compartida con el workflow de n8n.
- `PUBLIC_BASE_URL` — URL pública con la que EasyPanel expone este servicio,
  ej. `https://jotarondon8-video-editor.xxxxx.easypanel.host` (sin barra final).

## Despliegue en EasyPanel — paso a paso

Ver instrucciones completas en el mensaje de Claude Code. Resumen:

1. Crear un repo nuevo en GitHub con el contenido de esta carpeta.
2. En EasyPanel: Create Service → App → Source: GitHub → elegir el repo →
   Build: Dockerfile.
3. Configurar las dos variables de entorno de arriba.
4. Activar dominio público (botón "Enable Domain" / dominio `*.easypanel.host`).
5. Deploy. Verificar con `GET /health`.
