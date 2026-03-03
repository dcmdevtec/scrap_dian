import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from camoufox import AsyncCamoufox
from playwright_captcha.utils.camoufox_add_init_script.add_init_script import get_addon_path
from playwright_captcha import CaptchaType, ClickSolver, FrameworkType

logging.basicConfig(
    level='INFO',
    format='[%(asctime)s] {%(pathname)s:%(lineno)d} %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)

logger = logging.getLogger(__name__)

# Crear directorio para debug screenshots
DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'debug_screenshots')
os.makedirs(DEBUG_DIR, exist_ok=True)

# Crear directorio para descargas
DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'downloads')
os.makedirs(DOWNLOADS_DIR, exist_ok=True)


class CaptchaRequest(BaseModel):
    user_code: str
    company_code: str
    debug: bool = False


class ExportRequest(BaseModel):
    auth_url: str  # URL completa del token recibida por correo
    debug: bool = False


class CaptchaResponse(BaseModel):
    status: str
    user_code: str
    company_code: str
    success: bool = True
    message: Optional[str] = None
    debug_url: Optional[str] = None


class ExportResponse(BaseModel):
    status: str
    success: bool = True
    message: Optional[str] = None
    download_path: Optional[str] = None
    download_url: Optional[str] = None
    debug_url: Optional[str] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager para startup y shutdown."""
    # Startup: limpiar screenshots antiguas
    await cleanup_old_screenshots()
    yield
    # Shutdown: nada por ahora


app = FastAPI(title="Captcha Solver Service", lifespan=lifespan)

# Montar directorio de debug screenshots
app.mount('/debug', StaticFiles(directory=DEBUG_DIR), name='debug')

# Montar directorio de descargas
app.mount('/downloads', StaticFiles(directory=DOWNLOADS_DIR), name='downloads')

# Nota: Para detectar el host correcto detrás de reverse proxy,
# ejecutar uvicorn con: --proxy-headers --forwarded-allow-ips="*"

# Configuración
MAX_RETRIES = 3
RETRY_DELAY = 2  # segundos
PAGE_TIMEOUT = 30000  # 30 segundos
FORM_SUBMIT_TIMEOUT = 15000  # 15 segundos
DEBUG_EXPIRY_HOURS = 24  # Las imágenes de debug se eliminan después de 1 día
EXPORT_WAIT_INTERVAL = 50  # segundos entre cada verificación de exportación
EXPORT_MAX_WAIT_TIME = 600  # máximo 10 minutos esperando la exportación


async def cleanup_old_screenshots():
    """Elimina screenshots de debug más antiguas que DEBUG_EXPIRY_HOURS."""
    try:
        now = datetime.now()
        expiry = timedelta(hours=DEBUG_EXPIRY_HOURS)
        for filename in os.listdir(DEBUG_DIR):
            if filename.endswith('.png'):
                filepath = os.path.join(DEBUG_DIR, filename)
                file_mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
                if now - file_mtime > expiry:
                    os.remove(filepath)
                    logger.info(f'Debug screenshot eliminada: {filename}')
    except Exception as exc:
        logger.error(f'Error limpiando screenshots: {exc}')


async def solve_turnstile(
    user_code: str,
    company_code: str,
    debug: bool = False,
    retries: int = MAX_RETRIES,
    base_url: str = 'http://localhost:8000'
) -> Dict[str, Any]:
    """Perform the Cloudflare turnstile flow with the provided codes.

    Returns a dictionary that can be returned to the client.
    """
    ADDON_PATH = get_addon_path()
    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        logger.info(f"Intento {attempt}/{retries}")

        try:
            async with AsyncCamoufox(
                headless=True,
                geoip=True,
                humanize=True,
                i_know_what_im_doing=True,
                config={'forceScopeAccess': True},
                disable_coop=True,
                main_world_eval=True,
                addons=[os.path.abspath(ADDON_PATH)]
            ) as browser:
                context = await browser.new_context()
                page = await context.new_page()

                framework = FrameworkType.CAMOUFOX

                async with ClickSolver(framework=framework, page=page) as solver:
                    # Navegar y esperar carga completa
                    await page.goto(
                        'https://catalogo-vpfe.dian.gov.co/User/CompanyLogin',
                        wait_until='networkidle',
                        timeout=PAGE_TIMEOUT
                    )

                    # Esperar elemento del representante legal
                    await page.wait_for_selector(
                        '#legalRepresentative',
                        state='visible',
                        timeout=PAGE_TIMEOUT
                    )
                    await asyncio.sleep(1)  # Pequeña pausa para estabilidad

                    # Click para mostrar CAPTCHA
                    await page.click('#legalRepresentative')

                    await asyncio.sleep(1)  # Esperar que el widget se inicialice

                    # Resolver CAPTCHA
                    await solver.solve_captcha(
                        captcha_container=page,
                        captcha_type=CaptchaType.CLOUDFLARE_TURNSTILE
                    )

                    logger.info('CAPTCHA resuelto exitosamente')

                    # Verificar que el CAPTCHA fue resuelto (busca token o iframe desaparecido)
                    await asyncio.sleep(2)

                    # Rellenar formulario
                    await page.fill('#UserCode', user_code, timeout=PAGE_TIMEOUT)
                    await page.fill('#CompanyCode', company_code, timeout=PAGE_TIMEOUT)

                    # Click en submit y esperar navegación o resultado
                    await asyncio.gather(
                        page.wait_for_load_state('networkidle', timeout=FORM_SUBMIT_TIMEOUT),
                        page.click('.btn.btn-primary', timeout=5000),
                        return_exceptions=True
                    )

                    # Esperar navegación a LoginConfirmed
                    # try:
                    #     await page.wait_for_url('**/LoginConfirmed*', timeout=FORM_SUBMIT_TIMEOUT)
                    # except Exception:
                    #     # Si no hay navegación, verificar si estamos en la misma página con errores
                    #     pass

                    # Verificar éxito: buscar el div de confirmación
                    # success_selector = await page.wait_for_selector(
                    #     'dian-alert.dian-alert-info.mt-5c',
                    #     state='visible',
                    #     timeout=10000
                    # )
                    # if success_selector:
                    #     # Extraer mensaje de éxito del elemento p hijo
                    #     p_element = await success_selector.query_selector('p')
                    #     if p_element:
                    #         success_message = await p_element.inner_text()
                    #         logger.info(f'Mensaje de confirmación: {success_message}')
                    #

                    await asyncio.sleep(2)
                    
                    # Captura de pantalla después del submit (solo si debug=True)
                    debug_url = None
                    if debug:
                        unique_id = uuid.uuid4().hex[:12]
                        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                        screenshot_filename = f'debug_{timestamp}_{unique_id}.png'
                        screenshot_path = os.path.join(DEBUG_DIR, screenshot_filename)
                        await page.screenshot(path=screenshot_path, full_page=True)
                        debug_url = f'{base_url}/debug/{screenshot_filename}'
                        logger.info(f'Debug screenshot guardada: {screenshot_filename}')

                    # Verificar errores si no hay éxito
                    error_selector = await page.query_selector('.alert-danger, .text-danger, .error')
                    if error_selector:
                        error_text = await error_selector.inner_text()
                        if error_text and 'error' in error_text.lower():
                            raise Exception(f"Error en formulario: {error_text}")

                    logger.info('Formulario enviado correctamente')

            logger.info('Flujo completado exitosamente')
            return {
                "status": "ok",
                "user_code": user_code,
                "company_code": company_code,
                "success": True,
                "message": "CAPTCHA resuelto y formulario enviado",
                "debug_url": debug_url
            }

        except Exception as exc:
            last_error = exc
            logger.warning(f"Intento {attempt} fallido: {exc}")

            if attempt < retries:
                logger.info(f"Reintentando en {RETRY_DELAY} segundos...")
                await asyncio.sleep(RETRY_DELAY)
            else:
                logger.error(f"Todos los intentos fallaron. Último error: {exc}")

    raise last_error or Exception("Todos los intentos fallaron")


async def export_and_download(
    auth_url: str,
    debug: bool = False,
    retries: int = MAX_RETRIES,
    base_url: str = 'http://localhost:8000'
) -> Dict[str, Any]:
    """
    Flujo completo: Autenticar con URL del token -> Navegar a Export -> Exportar -> Esperar -> Descargar.
    
    Args:
        auth_url: URL completa del token recibida por correo 
                  (ej: https://catalogo-vpfe.dian.gov.co/User/AuthToken?pk=...&rk=...&token=...)
    
    Returns a dictionary with download info.
    """
    ADDON_PATH = get_addon_path()
    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        logger.info(f"Intento de exportación {attempt}/{retries}")

        try:
            async with AsyncCamoufox(
                headless=False,  # MODO VISIBLE para debug
                geoip=True,
                humanize=True,
                i_know_what_im_doing=True,
                config={'forceScopeAccess': True},
                disable_coop=True,
                main_world_eval=True,
                addons=[os.path.abspath(ADDON_PATH)]
            ) as browser:
                context = await browser.new_context(accept_downloads=True)
                page = await context.new_page()

                # ========== FASE 1: AUTENTICACIÓN CON TOKEN ==========
                logger.info('Fase 1: Autenticando con URL del token...')
                logger.info(f'URL: {auth_url}')
                
                await page.goto(
                    auth_url,
                    wait_until='networkidle',
                    timeout=PAGE_TIMEOUT
                )
                
                await asyncio.sleep(2)
                
                # Verificar que estamos autenticados (buscar elementos del dashboard)
                current_url = page.url
                logger.info(f'URL actual después de autenticación: {current_url}')
                
                # Screenshot de debug después de autenticación
                debug_url = None
                if debug:
                    unique_id = uuid.uuid4().hex[:12]
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    screenshot_filename = f'auth_{timestamp}_{unique_id}.png'
                    screenshot_path = os.path.join(DEBUG_DIR, screenshot_filename)
                    await page.screenshot(path=screenshot_path, full_page=True)
                    debug_url = f'{base_url}/debug/{screenshot_filename}'
                    logger.info(f'Debug screenshot (auth): {screenshot_filename}')

                # ========== FASE 2: NAVEGAR A EXPORT ==========
                logger.info('Fase 2: Navegando a página de exportación...')
                
                await page.goto(
                    'https://catalogo-vpfe.dian.gov.co/Document/Export',
                    wait_until='networkidle',
                    timeout=PAGE_TIMEOUT
                )

                # Esperar que cargue la página de exportación
                await page.wait_for_selector(
                    'button:has-text("Exportar"), input[value="Exportar"], .btn:has-text("Exportar")',
                    state='visible',
                    timeout=PAGE_TIMEOUT
                )
                logger.info('Página de exportación cargada')

                # ========== CONFIGURAR RANGO DE FECHAS ==========
                # Hacer click en el botón "<" para ir al inicio del mes
                logger.info('Configurando rango de fechas (desde inicio del mes)...')
                try:
                    # Click en el botón "<" para retroceder al inicio del mes
                    prev_button = await page.query_selector('button:has-text("<"), .btn:has-text("<")')
                    if prev_button:
                        await prev_button.click()
                        await asyncio.sleep(1)
                        logger.info('Rango de fechas ajustado al inicio del mes')
                except Exception as date_exc:
                    logger.warning(f'No se pudo ajustar el rango de fechas: {date_exc}')

                # Screenshot de debug antes de exportar
                if debug:
                    unique_id = uuid.uuid4().hex[:12]
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    screenshot_filename = f'export_before_{timestamp}_{unique_id}.png'
                    screenshot_path = os.path.join(DEBUG_DIR, screenshot_filename)
                    await page.screenshot(path=screenshot_path, full_page=True)
                    debug_url = f'{base_url}/debug/{screenshot_filename}'
                    logger.info(f'Debug screenshot (antes): {screenshot_filename}')

                # ========== FASE 3: CLICK EN EXPORTAR ==========
                logger.info('Fase 3: Iniciando exportación...')
                
                # Buscar y hacer click en el botón de exportar
                export_button = await page.query_selector(
                    'button:has-text("Exportar Excel"), input[value*="Exportar"], .btn:has-text("Exportar")'
                )
                if not export_button:
                    # Intentar con selector más específico
                    export_button = await page.query_selector('#btnExport, .btn-primary:has-text("Exportar")')
                
                if not export_button:
                    raise Exception("No se encontró el botón de Exportar")

                await export_button.click()
                logger.info('Click en Exportar Excel realizado')

                # Esperar que aparezca el modal de confirmación y que se cierre solo
                logger.info('Esperando confirmación del sistema...')
                await asyncio.sleep(5)  # Esperar a que el modal aparezca y se cierre automáticamente
                
                logger.info('Exportación iniciada, esperando a que el archivo esté listo...')

                # ========== FASE 4: ESPERAR Y DESCARGAR ==========
                logger.info('Fase 4: Esperando que la exportación esté lista...')
                
                download_path = None
                start_time = datetime.now()
                export_url = 'https://catalogo-vpfe.dian.gov.co/Document/Export'
                
                while (datetime.now() - start_time).total_seconds() < EXPORT_MAX_WAIT_TIME:
                    # Navegar a la página de exportación (más confiable que reload)
                    try:
                        await page.goto(export_url, wait_until='domcontentloaded', timeout=60000)
                        await asyncio.sleep(3)  # Esperar que cargue completamente
                    except Exception as nav_exc:
                        logger.warning(f'Error al navegar: {nav_exc}, reintentando...')
                        await asyncio.sleep(5)
                        continue
                    
                    # Screenshot de debug en cada iteración
                    if debug:
                        unique_id = uuid.uuid4().hex[:8]
                        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                        screenshot_filename = f'export_wait_{timestamp}_{unique_id}.png'
                        screenshot_path = os.path.join(DEBUG_DIR, screenshot_filename)
                        await page.screenshot(path=screenshot_path, full_page=True)
                        logger.info(f'Debug screenshot (esperando): {screenshot_filename}')
                    
                    # Verificar si hay filas en la tabla (ya no dice "Ninguna tarea de exportación")
                    no_data_text = await page.query_selector('text="Ninguna tarea de exportación disponible para mostrar"')
                    
                    if no_data_text:
                        elapsed = (datetime.now() - start_time).total_seconds()
                        logger.info(f'Tabla vacía aún... ({elapsed:.0f}s transcurridos)')
                        await asyncio.sleep(EXPORT_WAIT_INTERVAL)
                        continue
                    
                    # Hay datos en la tabla, buscar filas
                    rows = await page.query_selector_all('table tbody tr')
                    logger.info(f'Encontradas {len(rows)} filas en la tabla')
                    
                    if len(rows) > 0:
                        # Buscar en la primera fila (la más reciente) el botón/link de descarga
                        first_row = rows[0]
                        
                        # Verificar el estado en la fila (columna 7 = Estado)
                        estado_cell = await first_row.query_selector('td:nth-child(7)')
                        estado_text = ""
                        if estado_cell:
                            estado_text = await estado_cell.inner_text()
                            logger.info(f'Estado de la exportación: "{estado_text}"')
                        
                        # Buscar el link de descarga específico de DIAN
                        # URL: /Document/DownloadExportedZipFile?pk=...&rk=...
                        download_link = await first_row.query_selector(
                            'a[href*="DownloadExportedZipFile"], '
                            'a[href*="Download"], '
                            'td:last-child a[href], '
                            'td:last-child a'
                        )
                        
                        if download_link:
                            # Obtener y mostrar el href
                            href = await download_link.get_attribute('href')
                            logger.info(f'¡Enlace de descarga encontrado! URL: {href}')
                            
                            # Screenshot antes de descargar
                            if debug:
                                unique_id = uuid.uuid4().hex[:8]
                                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                                screenshot_filename = f'export_ready_{timestamp}_{unique_id}.png'
                                screenshot_path = os.path.join(DEBUG_DIR, screenshot_filename)
                                await page.screenshot(path=screenshot_path, full_page=True)
                                logger.info(f'Debug screenshot (listo): {screenshot_filename}')
                            
                            try:
                                # Intentar descargar
                                async with page.expect_download(timeout=60000) as download_info:
                                    await download_link.click()
                                
                                download = await download_info.value
                                
                                # Guardar archivo
                                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                                suggested_filename = download.suggested_filename or f'export_{timestamp}.zip'
                                download_path = os.path.join(DOWNLOADS_DIR, suggested_filename)
                                
                                await download.save_as(download_path)
                                logger.info(f'Archivo descargado exitosamente: {download_path}')
                                break
                                
                            except Exception as download_exc:
                                logger.warning(f'Error al intentar descargar: {download_exc}')
                                # Puede que el archivo aún no esté listo, seguir esperando
                        else:
                            logger.info('Fila encontrada pero sin botón de descarga visible aún...')
                    
                    elapsed = (datetime.now() - start_time).total_seconds()
                    logger.info(f'Esperando exportación... ({elapsed:.0f}s transcurridos)')
                    await asyncio.sleep(EXPORT_WAIT_INTERVAL)
                
                if not download_path:
                    # Screenshot final de debug si falló
                    if debug:
                        unique_id = uuid.uuid4().hex[:12]
                        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                        screenshot_filename = f'export_timeout_{timestamp}_{unique_id}.png'
                        screenshot_path = os.path.join(DEBUG_DIR, screenshot_filename)
                        await page.screenshot(path=screenshot_path, full_page=True)
                        debug_url = f'{base_url}/debug/{screenshot_filename}'
                    
                    raise Exception("Timeout esperando la exportación. El archivo no estuvo listo a tiempo.")

                # Screenshot final de éxito
                if debug:
                    unique_id = uuid.uuid4().hex[:12]
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    screenshot_filename = f'export_success_{timestamp}_{unique_id}.png'
                    screenshot_path = os.path.join(DEBUG_DIR, screenshot_filename)
                    await page.screenshot(path=screenshot_path, full_page=True)
                    debug_url = f'{base_url}/debug/{screenshot_filename}'

            logger.info('Exportación completada exitosamente')
            
            # Construir URL de descarga
            download_filename = os.path.basename(download_path)
            download_url = f'{base_url}/downloads/{download_filename}'
            
            return {
                "status": "ok",
                "success": True,
                "message": "Exportación completada y archivo descargado",
                "download_path": download_path,
                "download_url": download_url,
                "debug_url": debug_url
            }

        except Exception as exc:
            last_error = exc
            logger.warning(f"Intento de exportación {attempt} fallido: {exc}")

            if attempt < retries:
                logger.info(f"Reintentando en {RETRY_DELAY} segundos...")
                await asyncio.sleep(RETRY_DELAY)
            else:
                logger.error(f"Todos los intentos de exportación fallaron. Último error: {exc}")

    raise last_error or Exception("Todos los intentos de exportación fallaron")


@app.post('/solve', response_model=CaptchaResponse)
async def solve_endpoint(payload: CaptchaRequest, request: Request):
    try:
        # Construir base URL desde la request
        base_url = str(request.base_url).rstrip('/')
        result = await solve_turnstile(
            payload.user_code,
            payload.company_code,
            debug=payload.debug,
            base_url=base_url
        )
        return CaptchaResponse(**result)
    except Exception as exc:
        logger.exception('Error durante la resolución del CAPTCHA')
        raise HTTPException(
            status_code=500,
            detail={
                "error": str(exc),
                "type": type(exc).__name__
            }
        )


@app.post('/export', response_model=ExportResponse)
async def export_endpoint(payload: ExportRequest, request: Request):
    """
    Endpoint para exportar y descargar el listado de documentos.
    
    Recibe la URL de autenticación que el usuario obtiene por correo.
    Ejemplo: https://catalogo-vpfe.dian.gov.co/User/AuthToken?pk=...&rk=...&token=...
    """
    try:
        base_url = str(request.base_url).rstrip('/')
        result = await export_and_download(
            auth_url=payload.auth_url,
            debug=payload.debug,
            base_url=base_url
        )
        return ExportResponse(**result)
    except Exception as exc:
        logger.exception('Error durante la exportación')
        raise HTTPException(
            status_code=500,
            detail={
                "error": str(exc),
                "type": type(exc).__name__
            }
        )


if __name__ == '__main__':
    # allow running the service directly for quick tests
    import uvicorn

    uvicorn.run(
        'main:app',
        host='0.0.0.0',
        port=8000,
        log_level='info',
        proxy_headers=True,
        forwarded_allow_ips='*'
    )
