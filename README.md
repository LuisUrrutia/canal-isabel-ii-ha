# Canal de Isabel II para Home Assistant

Integración personalizada de Home Assistant para consultar automáticamente el contador y el consumo de agua disponibles en la Oficina Virtual de Canal de Isabel II.

> [!IMPORTANT]
> Este proyecto no es oficial ni está afiliado a Canal de Isabel II. La Oficina Virtual no ofrece una API pública conocida; la integración reproduce el comportamiento de su portal web, que puede cambiar sin previo aviso.

## Funciones

- Inicio de sesión automático con NIF/NIE y contraseña.
- Resolución del reCAPTCHA invisible mediante 2Captcha.
- Detección automática de todos los contratos de la cuenta.
- Descarga inicial de aproximadamente seis meses de consumos diarios y horarios.
- Historial privado persistente y sincronizaciones incrementales con corrección de los dos últimos días.
- Sincronización diaria a una hora configurable (03:00 de forma predeterminada).
- Un dispositivo por contrato y tres sensores: contador, consumo horario y consumo diario.
- Importación del histórico horario en las estadísticas de larga duración de Home Assistant.
- Diagnósticos sin credenciales, contratos, contadores ni direcciones.
- Traducciones en español e inglés.

## Requisitos

- Home Assistant 2026.8.0 o posterior.
- Una cuenta de la Oficina Virtual con acceso a telelecturas.
- Una [cuenta de 2Captcha](https://2captcha.com/) con saldo y una clave API.
- HACS para la instalación recomendada.
- Recorder habilitado para importar el histórico en las estadísticas de larga duración.

2Captcha es un servicio de pago ajeno a este proyecto. La integración envía a 2Captcha la clave pública del desafío y la URL de acceso, pero no le envía tu NIF/NIE ni tu contraseña.

## Instalación

### HACS

1. Abre **HACS > Integraciones**.
2. En el menú superior, selecciona **Repositorios personalizados**.
3. Añade la URL de este repositorio como categoría **Integración**.
4. Busca **Canal de Isabel II** y selecciona **Descargar**.
5. Reinicia Home Assistant.

### Manual

1. Copia `custom_components/canal_de_isabel_ii` dentro de `config/custom_components/`.
2. Reinicia Home Assistant.

## Configuración

1. Crea una cuenta en 2Captcha, añade saldo y copia su clave API.
2. En Home Assistant, abre **Ajustes > Dispositivos y servicios**.
3. Selecciona **Añadir integración** y busca **Canal de Isabel II**.
4. Introduce el NIF o NIE, la contraseña de la Oficina Virtual y la clave API de 2Captcha.

La integración valida el acceso y descubre todos los contratos disponibles en esa cuenta. Se pueden configurar varias cuentas; un mismo NIF/NIE no puede añadirse dos veces.

La primera sincronización descarga hasta seis meses. Como el portal solo permite consultar los consumos horarios día a día, esta operación puede tardar varios minutos. Las sincronizaciones siguientes solo vuelven a consultar el tramo reciente.

## Prueba real desde CLI

Antes de instalar la integración se puede comprobar el flujo completo contra el portal:

```bash
./scripts/test_live.sh
```

El asistente solicita el NIF/NIE, la contraseña y la API key de 2Captcha con entrada oculta. Los valores solo se exportan al subproceso de prueba: no se escriben en `.env`, argumentos, configuración ni logs.

La prueba usa el mismo `CanalClient` que Home Assistant y muestra, para cada contrato:

- la lectura actual del contador;
- todos los consumos diarios del mes natural del último dato disponible;
- las lecturas horarias del último día disponible y su suma.

Para mantenerla rápida, consulta como máximo 31 días diarios y solo un día horario. Resolverá un CAPTCHA real y puede consumir saldo de 2Captcha.

## Entidades

Cada contrato crea un dispositivo con estos sensores:

| Sensor | Unidad | Uso |
| --- | --- | --- |
| Lectura del contador | m³ | Total físico acumulado y fuente recomendada para el panel de Energía |
| Consumo horario | L | Consumo del intervalo horario más reciente |
| Consumo diario | L | Consumo del último día disponible |

El sensor horario conserva el `unique_id` de versiones anteriores para evitar duplicarlo durante una actualización.

### Panel de Energía

1. Espera a que finalice la primera sincronización.
2. Abre **Ajustes > Paneles > Energía**.
3. En **Consumo de agua**, selecciona el sensor **Lectura del contador** del contrato.

La integración reconstruye el histórico acumulado anclándolo a la lectura física publicada por el portal e importa las correcciones sin duplicar puntos.

## Sincronización y almacenamiento

- La primera ejecución solicita aproximadamente 183 días.
- Las consultas diarias se dividen por meses para evitar un defecto del portal al cruzar meses.
- Las consultas horarias se realizan un día cada vez.
- Después de la primera ejecución se actualizan el último tramo y los dos días de corrección.
- El historial se guarda mediante el almacenamiento privado y atómico de Home Assistant.
- La sincronización se ejecuta una vez al día a las 03:00, salvo que se cambie desde **Configurar**.
- Si el portal rechaza el acceso, Home Assistant inicia una reautenticación.

## Actualización desde una versión con `JSESSIONID`

La versión 3 sustituye la cookie manual por el inicio de sesión automático. La entrada anterior se conserva, pero Home Assistant solicitará una vez el NIF/NIE, la contraseña y la clave API de 2Captcha. Después ya no será necesario copiar cookies.

## Seguridad y privacidad

- Las contraseñas y claves API nunca aparecen en los diagnósticos.
- El historial se guarda como almacenamiento privado de Home Assistant.
- Cada cuenta utiliza su propio contenedor de cookies en memoria.
- No publiques el archivo de configuración, los registros con depuración ni el contenido del almacenamiento privado.

## Limitaciones conocidas

- El portal no ofrece una API pública estable; cambios en sus formularios o gráficos pueden requerir una actualización de la integración.
- La disponibilidad, el retraso y la precisión de las telelecturas dependen de Canal de Isabel II.
- El inicio de sesión automático depende de un servicio externo de resolución de CAPTCHA.
- Sin Recorder, los tres sensores funcionan, pero no se importa el histórico acumulado.

## Solución de problemas

### Credenciales rechazadas

Comprueba que puedes acceder a la Oficina Virtual con el mismo NIF/NIE y contraseña. Después abre la notificación de reautenticación de Home Assistant.

### Error de CAPTCHA

Comprueba la clave API y el saldo de 2Captcha. Si la cuenta es válida, inténtalo de nuevo unos minutos más tarde.

### El portal devuelve una respuesta no compatible

Comprueba que la web permite abrir manualmente **Telelecturas**. Si funciona, descarga los diagnósticos desde la integración y adjúntalos a una incidencia; no contienen identificadores del suministro.

### No aparece el histórico

Comprueba que Recorder está habilitado y espera a que termine la primera sincronización. La descarga inicial hace una consulta por cada día disponible y puede tardar.

## Eliminación

1. Elimina **Canal de Isabel II** desde **Ajustes > Dispositivos y servicios**.
2. Desinstala el repositorio desde HACS.
3. Reinicia Home Assistant si HACS lo solicita.

## Desarrollo

El proyecto usa Python 3.14 y `uv`:

```bash
uv sync --locked --all-groups
uv run ruff format --check .
uv run ruff check .
uv run pytest --cov=custom_components/canal_de_isabel_ii --cov-fail-under=95
```

La CI valida además el repositorio con Hassfest y HACS.

## Licencia

[MIT](LICENSE)
