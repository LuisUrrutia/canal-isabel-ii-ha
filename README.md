# Canal de Isabel II para Home Assistant

Integración personalizada para Home Assistant que consulta el contador y las telelecturas de la Oficina Virtual de Canal de Isabel II. Conserva el historial, publica estadísticas para el Panel de Energía y permite estimar el coste del ciclo de facturación.

> [!IMPORTANT]
> Este proyecto no es oficial ni está afiliado a Canal de Isabel II. La Oficina Virtual no ofrece una API pública conocida; la integración reproduce el comportamiento de su portal web, que puede cambiar sin previo aviso.

## Origen e inspiración

Este proyecto nació inspirado por [la integración de miguelangel-nubla](https://github.com/miguelangel-nubla/homeassistant_canal_isabel_II), cuyo trabajo demostró que era posible incorporar las telelecturas de Canal de Isabel II a Home Assistant y sirvió como referencia inicial.

La integración original resuelve el acceso mediante una cookie `JSESSIONID` obtenida manualmente. A partir de esa base, preferí explorar una experiencia más automatizada: este proyecto inicia sesión con las credenciales del usuario, renueva la sesión cuando es necesario y organiza la extracción, la persistencia y la presentación de los datos en módulos independientes.

## Funciones

- Inicio de sesión automático con NIF/NIE y contraseña.
- Resolución del reCAPTCHA invisible mediante 2Captcha, con cinco intentos predeterminados y un límite configurable entre uno y diez.
- Detección automática de todos los contratos de la cuenta.
- Descarga inicial de hasta 183 días de consumos diarios y horarios.
- Alta inmediata en Home Assistant y sincronización inicial en segundo plano.
- Historial privado persistente que no descarta lecturas anteriores.
- Sincronizaciones incrementales con corrección de los dos últimos días.
- Resincronización completa y no destructiva desde la configuración de la integración.
- Sincronización diaria a una hora configurable (03:00 de forma predeterminada).
- Un dispositivo por contrato y cuatro sensores: contador, consumo horario, consumo diario y factura estimada.
- Cálculo opcional de la factura estimada con el tarifario oficial 2026, bloques prorrateados, temporadas, cuotas fijas, alcantarillado e IVA.
- Importación del histórico horario en una estadística externa independiente de las estadísticas automáticas de Recorder.
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

Home Assistant guarda la integración inmediatamente. La validación del acceso, el descubrimiento de contratos y la primera descarga se ejecutan después en segundo plano. Puedes configurar varias cuentas, pero un mismo NIF/NIE no puede añadirse dos veces. Home Assistant solo solicita una reautenticación cuando rechaza las credenciales o cuando la cuenta de 2Captcha requiere intervención. Los fallos transitorios conservan los datos y se reintentan más tarde.

La primera sincronización descarga hasta 183 días. Como el portal solo permite consultar los consumos horarios día a día, esta operación puede tardar varios minutos. Durante ese tiempo la integración ya aparece en Home Assistant y las entidades se crean en cuanto se obtiene el primer conjunto completo de datos. Las sincronizaciones siguientes solo vuelven a consultar el tramo reciente.

### Configuración de la sincronización

Abre **Configurar > Horario de sincronización** para cambiar:

- La hora local de la sincronización diaria
- El número máximo de intentos de CAPTCHA por inicio de sesión, entre uno y diez

El valor predeterminado es cinco intentos. Los errores permanentes de cuenta, como una clave inválida o saldo insuficiente, detienen los reintentos inmediatamente.

Selecciona **Configurar > Resincronizar todo el historial** para actualizar manualmente todas las lecturas conservadas. La tarea se ejecuta en segundo plano y mantiene disponibles los datos existentes. El rango comienza en la lectura más antigua guardada o 183 días antes del último dato publicado, lo que resulte más antiguo.

### Configuración de precios

El portal de consumos no publica todos los datos necesarios para reconstruir una
factura. Después de la primera sincronización, abre **Configurar > Precio del
agua**, elige un contrato e introduce:

- tipo de suministro doméstico;
- prestador del alcantarillado: Canal o ayuntamiento;
- diámetro del contador;
- número de viviendas, locales o usos abastecidos (`N`);
- fecha final de la última factura, que pasa a ser el inicio del periodo actual;
- duración nominal del ciclo de facturación;
- tarifa municipal de alcantarillado, si la cobra el ayuntamiento.

Estos perfiles se guardan en almacenamiento privado, separado de las credenciales
y del historial. La integración incluye las tarifas domésticas 2026 publicadas por
Canal de Isabel II. Cuando cambie el tarifario será necesaria una actualización de
la integración; no se interpreta el PDF durante la ejecución.

El resultado es una estimación en curso: usa el consumo diario publicado hasta la
última fecha disponible y un ciclo nominal configurado. La factura definitiva puede
variar si Canal usa otras fechas de lectura, corrige consumos o aplica conceptos no
incluidos en el tarifario.

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
| Lectura del contador | m³ | Última lectura física acumulada publicada por el portal |
| Consumo horario | L | Consumo del intervalo horario más reciente |
| Consumo diario | L | Consumo del último día disponible |
| Factura de agua estimada | € | Coste acumulado del periodo; aparece al configurar los precios |

El sensor horario conserva el `unique_id` de versiones anteriores para evitar duplicarlo durante una actualización.

### Panel de Energía

1. Espera a que finalice la primera sincronización.
2. Abre **Ajustes > Paneles > Energía**.
3. En **Consumo de agua**, selecciona la estadística externa cuyo nombre comienza
   por **Canal de Isabel II ·**. Su identificador comienza por
   `canal_de_isabel_ii:water_meter_`.

No selecciones la estadística automática del sensor **Lectura del contador**: esa
serie solo empieza cuando Home Assistant registra la entidad y no contiene el
histórico del portal. La integración reconstruye el histórico acumulado,
anclándolo a la lectura física, dentro de una estadística externa que ningún otro
componente modifica.

Al actualizar desde 3.1.1 o una versión anterior, elimina del panel la fuente de
agua anterior y selecciona la nueva estadística externa. Si la fuente anterior
llegó a mostrar un consumo negativo, elimina únicamente sus estadísticas desde
**Herramientas para desarrolladores > Estadísticas** después de cambiar la fuente.

Si se configuraron precios, edita la misma fuente de agua y selecciona la
estadística externa cuyo nombre termina en **cost estimate** como estadística de
coste. Su identificador comienza por `canal_de_isabel_ii:water_cost_`. Esta serie
reconstruye también el coste histórico y permanece creciente entre ciclos de
facturación. El sensor **Factura de agua estimada** queda disponible para consultar
el total y el desglose del periodo actual.

No uses un precio fijo por metro cúbico: los bloques progresivos, las temporadas y
las cuotas de servicio hacen que ese cálculo no coincida con la factura.

## Sincronización y almacenamiento

- La primera ejecución solicita aproximadamente 183 días.
- Las consultas diarias se dividen por meses para evitar un defecto del portal al cruzar meses.
- Las consultas horarias se realizan un día cada vez.
- Después de la primera ejecución se consultan los días pendientes y los dos últimos días, que pueden recibir correcciones del portal.
- El historial se guarda mediante el almacenamiento privado y atómico de Home Assistant. Las lecturas ya almacenadas no se eliminan si el portal omite una fila.
- La sincronización se ejecuta una vez al día a las 03:00, salvo que se cambie desde **Configurar**.
- La resincronización manual recorre todo el historial retenido, con una ventana mínima de 183 días.
- Si el portal rechaza las credenciales o la cuenta de 2Captcha requiere intervención, Home Assistant inicia una reautenticación.
- Los errores transitorios de CAPTCHA se reintentan hasta el límite configurado y no ocultan el último conjunto válido de datos.
- Si la sesión del portal caduca durante la descarga, la integración inicia una sesión nueva con CAPTCHA y reintenta la descarga completa una vez.

## Registros

La integración registra en inglés el alta, la carga de caché, la autenticación, la resolución del CAPTCHA, el progreso por contrato, los rangos diarios, cada día horario, el almacenamiento y la clasificación de errores. Los hitos y errores aparecen con el nivel normal; para ver el detalle de cada petición y consulta, añade temporalmente:

```yaml
logger:
  logs:
    custom_components.canal_de_isabel_ii: debug
```

Después reinicia Home Assistant y consulta **Ajustes > Sistema > Registros**. Las trazas de la integración no incluyen el NIF/NIE, la contraseña, la API key, tokens, cookies ni el HTML recibido. Aun así, revisa cualquier registro antes de publicarlo.

## Seguridad y privacidad

- Las contraseñas y claves API nunca aparecen en los diagnósticos.
- El historial se guarda como almacenamiento privado de Home Assistant.
- Cada cuenta utiliza su propio contenedor de cookies en memoria.
- No publiques el archivo de configuración, los registros con depuración ni el contenido del almacenamiento privado.

## Limitaciones conocidas

- El portal no ofrece una API pública estable; cambios en sus formularios o gráficos pueden requerir una actualización de la integración.
- La disponibilidad, el retraso y la precisión de las telelecturas dependen de Canal de Isabel II.
- El inicio de sesión automático depende de un servicio externo de resolución de CAPTCHA.
- Sin Recorder, los sensores funcionan, pero no se importa el histórico acumulado.
- El cálculo de precios incluido actualmente cubre usos domésticos y asimilados al
  doméstico durante 2026. Los usos comerciales, industriales y otros usos todavía
  no se calculan.
- La factura estimada usa ciclos nominales; no sustituye a la factura emitida por
  Canal ni incluye bonificaciones o conceptos extraordinarios.

## Solución de problemas

### Credenciales rechazadas

Comprueba que puedes acceder a la Oficina Virtual con el mismo NIF/NIE y contraseña. Después abre la notificación de reautenticación de Home Assistant.

### Error de CAPTCHA

Comprueba la clave API y el saldo de 2Captcha. La integración reintenta automáticamente los errores transitorios hasta el límite configurado. Si la cuenta es válida y el problema continúa, espera unos minutos antes de iniciar una resincronización manual.

### El portal devuelve una respuesta no compatible

Comprueba que la web permite abrir manualmente **Telelecturas**. Si funciona, descarga los diagnósticos desde la integración y adjúntalos a una incidencia; no contienen identificadores del suministro.

### No aparece el histórico

Comprueba que Recorder está habilitado y espera a que termine la primera sincronización en segundo plano. La descarga inicial hace una consulta por cada día disponible y puede tardar. Activa temporalmente los registros de depuración anteriores para seguir su progreso.

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

La integración conserva en su raíz únicamente los puntos de entrada reconocidos por
Home Assistant. La implementación se agrupa en tres módulos internos:

- `portal`: autenticación, CAPTCHA, navegación y normalización de la Oficina Virtual.
- `consumption`: modelos, almacenamiento y estadísticas de consumo.
- `billing`: cálculo tarifario versionado y almacenamiento de perfiles de facturación.

Los módulos exponen sus interfaces desde sus respectivos `__init__.py`; el resto de
la implementación se considera privada y puede cambiar sin afectar a los puntos de
entrada de Home Assistant.

La CI valida además el repositorio con Hassfest y HACS.

## Licencia

[MIT](LICENSE)
