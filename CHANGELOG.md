# Cambios — Asistente

Documentación generada automáticamente por el agente IA `Documentar cambios Git` (pmb_devops). Los registros más recientes aparecen al inicio del bloque.

<!-- AGENT:CHANGES:START -->
## 2026-04-18 13:02 UTC — 3 commit(s)

# Documentación de Cambios — Asistente (pmb_devops)

Rama: `HEAD` · Commits nuevos: 3

---

## 🚀 Nuevas Funcionalidades

### Prompt de sistema personalizado para agentes IA
**Commit:** `c56e4c5` — *pmb_devops 19.0.1.1.16*

Se añade soporte para configurar un **system prompt personalizado** y un **límite de commits** (`max_commits`) por agente IA en el modelo `devops.ai.agent`.

- Nuevo endpoint `/devops/agent/update` para actualizar agentes existentes.
- El endpoint `/devops/agent/create` ahora acepta los parámetros `custom_system_prompt` y `max_commits`.
- El listado de agentes (`agent_list`) expone ambos campos al frontend.
- Interfaz de usuario actualizada (`pmb_app.js` y `pmb_app.xml`) para permitir editar estos campos desde la aplicación.

---

## 🔧 Correcciones

### Corrección de IDs de modelos de Copilot
**Commit:** `9acfbbd` — *pmb_devops 19.0.1.1.17*

Se actualiza el catálogo de modelos disponibles en `copilot_model` para reflejar los IDs reales soportados por GitHub Copilot Chat:

- **Eliminados** (IDs incorrectos/obsoletos): `claude-sonnet-4`, `claude-opus-4.6`, `gpt-5.2`, `gemini-2.5-pro`.
- **Añadidos**: `gpt-4.1`, `gpt-5-mini`, `claude-haiku-4.5`, `claude-sonnet-4.6`, `claude-opus-4.7`, `gpt-5.4`, `gemini-3.1-pro-preview`.
- Nuevo modelo por defecto: `gpt-4o` (anteriormente `claude-sonnet-4`, ID inválido).
- Se identifican los modelos que requieren plan superior en sus etiquetas.

---

## ✨ Mejoras

### Cambio de modelo por defecto a Claude Opus 4.7
**Commit:** `8a2ede4` — *pmb_devops 19.0.1.1.18*

Se reorganiza la lista de modelos de Copilot priorizando los modelos Claude y se establece **Claude Opus 4.7** como modelo predeterminado.

- Reordenación de la `Selection`: primero los modelos Claude, luego GPT y Gemini.
- Se eliminan las anotaciones “(plan superior)” de las etiquetas para una presentación más limpia.
- Fallback en `_call_copilot` actualizado: `claude-opus-4.7` en lugar de `gpt-4o`.

---

## 📌 Resumen de versiones

| Versión       | Commit    | Descripción breve                          |
|---------------|-----------|--------------------------------------------|
| `19.0.1.1.16` | `c56e4c5` | System prompt personalizado en agentes IA  |
| `19.0.1.1.17` | `9acfbbd` | Corrección de IDs de modelos Copilot       |
| `19.0.1.1.18` | `8a2ede4` | Claude Opus 4.7 como modelo por defecto    |

---

## 2026-04-18 13:01 UTC — 20 commit(s)

# Documentación de Cambios — pmb_devops

Resumen de los últimos 20 commits aplicados al módulo **PatchMyByte DevOps** (versiones `19.0.1.1.0` → `19.0.1.1.18`).

---

## 🚀 Nuevas Funcionalidades

### Agentes IA y Asistentes
- **`70bb591`** — Sistema completo de **agentes de IA** (`devops.ai.agent`): ejecución programada, integración con GitHub Copilot, workspace dedicado por agente con `CLAUDE.md` inyectado, gestión de sesiones y vinculación con commits (`devops.commit.link`).
- **`c56e4c5`** — Soporte para **prompt de sistema personalizado** por agente IA y configuración de `max_commits`. Nuevo endpoint `/devops/agent/update`.
- **`0355528`** — El prompt del **chat IA** ahora utiliza el editor HTML con soporte para **pegar imágenes**. Cuando hay imágenes adjuntas se fuerza el uso de la API (vía `claude_api_key`) en lugar del CLI.
- **`1dac014`** — Descripción de tareas (`project.task`) usa el editor **Wysiwyg de `@html_editor`**, lo que permite subir imágenes pegadas como adjuntos (`/web/image/<id>`) en vez de data-URIs.

### Tareas y Etapas
- **`ea527fe`** — Etapas DevOps automáticas (`Levantamiento`, `Development`, `Staging`, `Producción`), doble asignación y soporte para usuarios de producción.
- **`6f9ab49`** — **Flujo de aprobación de tareas** enviadas por cliente (etapa `Pendiente de revisión`) con endpoint `/devops/project/task/approve` (acciones `approve`/`reject`).
- **`29a91ec`** — **Vinculación bidireccional** entre tareas y reuniones, con indicadores de audio/transcripción.
- **`3da2c9b`** — Endpoint `/devops/project/resync_tasks`: re-sincroniza tareas locales sin `pmb_remote_task_id` hacia producción.
- **`3fedda7`** — Endpoint `/devops/project/resync_stages`: sincronización masiva y validación de etapas en producción antes de sync de tareas.

### Reuniones
- **`7584499`** — **Soporte para grabaciones rotativas**: parámetro `rotating` en `/devops/meetings/upload_chunk` permite cerrar grabaciones intermedias sin terminar la sesión.

### Otros
- **`70bb591`** — Panel de módulos con **upgrade asíncrono**, script `module_op.sh`, modelo `devops_instance_infra` ampliado, mapeo de etapas (`devops_stage_mapping`).

---

## 🐛 Correcciones

- **`8a2ede4`** — El modelo por defecto de Copilot ahora es **Claude Opus 4.7** (antes `gpt-4o`).
- **`9acfbbd`** — Corregidos los IDs de modelos disponibles en GitHub Copilot (eliminado `claude-sonnet-4`/`claude-opus-4.6` inexistentes; añadidos `claude-haiku-4.5`, `gpt-4.1`, `gpt-5.4`, `gemini-3.1-pro-preview`).
- **`f0ddb16`** — El enlace de **debug de instancias** ahora apunta a `/odoo` para evitar que el redirect de `/web` descarte el flag `debug=assets`.
- **`05a446a`** — El **post-clone script** ahora se ejecuta como Python en el shell de Odoo de la instancia (antes se ejecutaba erróneamente como SQL).
- **`7287086`** — El input de filtro de la barra lateral ya no es **autocompletado** por el navegador (`autocomplete="off"`, `data-lpignore`, `data-1p-ignore`).
- **`c390b07`** — Corregido el **pegado de imágenes/archivos en la terminal**: cualquier ítem `file` toma precedencia (antes el texto adjunto bloqueaba la subida).
- **`8d3357a`** — Corregido `SyntaxError` en el extractor de imágenes del wizard de IA: reemplazado `nonlocal attachments` por una lista de IDs.
- **`b8b26cd`** — Eliminar tareas como **admin** ya no se bloquea por el chequeo de `unlink` con `sudo`. La validación de permisos se hace ahora a nivel de controlador, antes del sudo.
- **`7584499`** — **División de grabaciones grandes** antes de enviarlas a Groq Whisper (que tiene límite de tamaño).
- **`2a24e76`** — Corrección en `devops_instance_infra`: se elimina el prefijo `path=` del `ExecStart` de systemctl al detectar el binario Python en instancias SSH; fallback a `/usr/bin/python3` si no es ruta absoluta.

---

## ✨ Mejoras

- **`23a1eb7`** — **Internacionalización**: todos los encabezados y etiquetas de la SPA traducidos al español (`Branches → Ramas`, `Builds → Despliegues`, `Status → Estado`, `Logs → Registros`).
- **`70bb591`** — Cambios masivos a la SPA (`pmb_app.js/.xml`): nuevas vistas, controles de agentes, mejoras de UX (~2500 líneas añadidas).

---

## 🔧 Refactorizaciones

- **`05a446a`** — Reemplazo de la ejecución del script de post-clone (de `psql` directo a un helper `_run_python_in_instance_shell` reusable).
- **`8d3357a`** — Refactor del extractor de imágenes en `ai_assistant_wizard.py`: uso de lista mutable en lugar de `nonlocal` para evitar problemas de scoping.
- **`70bb591`** — Reorganización de utilidades: nuevo `utils/agent_workspace.py`, ampliación de `utils/git_utils.py` y `utils/ws_terminal.py`.

---

## 📌 Cambios de Versión

| Commit | Versión |
|---|---|
| `70bb591` | `19.0.1.1.0` → `19.0.1.1.4` |
| `7584499` | `19.0.1.1.4` → `19.0.1.1.6` |
| `3da2c9b` | `19.0.1.1.7` |
| `3fedda7` | `19.0.1.1.8` |
| `b8b26cd` | `19.0.1.1.9` |
| `0355528` | `19.0.1.1.10` |
| `1dac014` | `19.0.1.1.11` |
| `c390b07` | `19.0.1.1.12` |
| `7287086` | `19.0.1.1.13` |
| `05a446a` | `19.0.1.1.14` |
| `f0ddb16` | `19.0.1.1.15` |
| `c56e4c5` | `19.0.1.1.16` |
| `9acfbbd` | `19.0.1.1.17` |
| `8a2ede4` | `19.0.1.1.18` |

---

<!-- AGENT:CHANGES:END -->
