# bot_sistema_sefaz.py
import os
import time
import shutil
from datetime import datetime
from dotenv import load_dotenv
from captcha_solver import resolver_captcha
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException

import diagnostico as diag
from errors import (
    BotPlanilhaError,
    CaptchaFailedError,
    CredentialInvalidError,
    IpBlockedError,
    JobTimeoutError,
    PortalDownError,
    ShareUnavailableError,
)

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from webdriver_manager.chrome import ChromeDriverManager

# Carregar variáveis de ambiente de um arquivo .env
load_dotenv()


def configurar_driver(logger, download_dir):
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    try:
        options = ChromeOptions()

        options.add_argument('--ignore-certificate-errors')
        options.add_argument('--ignore-ssl-errors')
        options.add_argument('--disable-popup-blocking')
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--start-maximized")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--no-sandbox")

        prefs = {
            "download.default_directory": os.path.abspath(download_dir),
            "profile.default_content_settings.popups": 0,
            "directory_upgrade": True,
            "safeBrowse.enabled": True,
        }
        options.add_experimental_option("prefs", prefs)

        # Headless por env (default false). Em container Docker setar HEADLESS=true.
        if os.getenv("HEADLESS", "false").strip().lower() in ("1", "true", "yes"):
            options.add_argument("--headless=new")

        # Binário do Chrome via env (CHROME_BINARY). Em WSL costuma ser
        # /snap/bin/chromium; em Docker, /usr/bin/google-chrome.
        chrome_binary = os.getenv("CHROME_BINARY", "").strip()
        if chrome_binary:
            options.binary_location = chrome_binary

        # ChromeDriver via env (CHROMEDRIVER_PATH). Em ambientes onde já existe
        # um chromedriver gerenciado pelo SO (apt/imagem Docker), use ele direto.
        # Fallback: webdriver-manager (comportamento original, útil no Windows).
        chromedriver_path = os.getenv("CHROMEDRIVER_PATH", "").strip()
        if chromedriver_path:
            service = ChromeService(chromedriver_path)
            logger.info(f"Usando ChromeDriver de CHROMEDRIVER_PATH={chromedriver_path}")
        else:
            from webdriver_manager.chrome import ChromeDriverManager
            driver_path = ChromeDriverManager().install()
            service = ChromeService(driver_path)
            logger.info(f"Usando ChromeDriver baixado por webdriver-manager: {driver_path}")

        logger.info(f"Configurando ChromeDriver com diretório de download: {os.path.abspath(download_dir)}")
        driver = webdriver.Chrome(service=service, options=options)
        wait = WebDriverWait(driver, 60)

        return driver, wait

    except Exception as e:
        logger.error(f"Erro ao configurar o ChromeDriver: {e}")
        raise

def abrir_sefaz_dest(driver, logger, razao_social=None):
    sefaz_dest = os.getenv('SEFAZ_DEST')
    driver.get(sefaz_dest)
    verificar_erro_site(driver, logger, razao_social=razao_social)

def abrir_sefaz_emit(driver, logger, razao_social=None):
    sefaz_remet = os.getenv('SEFAZ_REMET')
    driver.get(sefaz_remet)
    verificar_erro_site(driver, logger, razao_social=razao_social)
    
def sanitizar_nome(nome):
    """
    Remove caracteres inválidos do nome para uso em diretórios.
    """
    return "".join(char for char in nome if char.isalnum() or char in " -_").strip()

def fazer_login(driver, razao_social, wait, logger, login, senha, run_id=None, tipo=None):
    try:
        # Converter login e senha para strings
        login = str(login)
        senha = str(senha)

        # Localizar e preencher o campo de login
        campo_login = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, '[id="PHCentro_userLogin"]')))
        campo_login.send_keys(login)
        logger.info(f"Login para a empresa {razao_social} obtido.")

        # Localizar e preencher o campo de senha
        campo_senha = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, '[id="PHCentro_userPass"]')))
        campo_senha.send_keys(senha)
        logger.info(f"Senha para a empresa {razao_social} obtida.")

        # Localizar e clicar no botão "Entrar"
        btn_entrar = driver.find_element(By.XPATH, '/html/body/form/section/div[2]/div[2]/div[1]/div[2]/div/div[3]/div/label')
        btn_entrar.click()
        logger.info("Botão 'Entrar' clicado com sucesso.")

        try:
            WebDriverWait(driver, 2).until(
                EC.visibility_of_element_located((By.ID, 'msgDetalhesErro'))
            )
        except TimeoutException:
            logger.info("Login realizado com sucesso")
            diag.evento(run_id, razao_social, tipo, "login", "ok")
            return True

        logger.error(f"Usuário ou senha inválidos para a empresa {razao_social}.")
        diag.evento(run_id, razao_social, tipo, "login", "fail",
                    erro="credenciais_invalidas",
                    extras={"motivo": "msgDetalhesErro visivel"})
        diag.salvar_evidencia(driver, run_id, razao_social, tipo, "login", sufixo="credenciais_invalidas")
        raise CredentialInvalidError(
            f"Usuário ou senha inválidos no portal SEFAZ para a empresa {razao_social}",
            empresa=razao_social,
        )

    except BotPlanilhaError:
        raise
    except Exception as e:
        logger.error(f"Erro ao tentar fazer login: {e}")
        diag.evento(run_id, razao_social, tipo, "login", "fail",
                    erro=f"{type(e).__name__}: {e}")
        diag.salvar_evidencia(driver, run_id, razao_social, tipo, "login", sufixo="exception")
        raise

# Heurística de IP block: o SEFAZ-BA não expõe um marcador único quando bloqueia
# o IP — varia entre "muitas tentativas", "acesso negado", "403", etc. Lista
# conservadora; só dispara quando ≥2 sinais batem no texto da página (reduz
# falso positivo do "OPS!" genérico, que cai como PORTAL_DOWN).
_IP_BLOCK_SIGNAIS = (
    "bloqueado",
    "ip bloqueado",
    "muitas tentativas",
    "tentativas excedidas",
    "acesso negado",
    "403",
    "forbidden",
)


def _detectar_ip_block(body_text: str) -> bool:
    if not body_text:
        return False
    lower = body_text.lower()
    hits = sum(1 for s in _IP_BLOCK_SIGNAIS if s in lower)
    return hits >= 2


def verificar_erro_site(driver, logger, razao_social=None):
    """Inspeciona o body do portal e levanta exceção tipada quando reconhece erro.

    - 2+ sinais de IP block (palavras-chave heurísticas) → IpBlockedError.
    - 'OPS!' (mensagem padrão de falha do SEFAZ-BA) → PortalDownError.
    - Body ausente / sem padrão conhecido → return None (caller segue).
    """
    try:
        body = driver.find_element(By.TAG_NAME, 'body').text
    except NoSuchElementException:
        return

    if _detectar_ip_block(body):
        logger.error(f"IP block detectado no portal SEFAZ-BA (razao_social={razao_social})")
        raise IpBlockedError(
            f"IP possivelmente bloqueado pelo SEFAZ-BA (heurística de página) "
            f"— empresa {razao_social}",
            empresa=razao_social,
        )

    if "OPS!" in body:
        logger.error(f"Portal SEFAZ-BA retornou 'OPS!' (razao_social={razao_social})")
        raise PortalDownError(
            f"Portal SEFAZ-BA fora do ar (mensagem 'OPS!') — empresa {razao_social}",
            empresa=razao_social,
        )

def preencher_formulario(driver, wait, logger, data_inicio, data_fim, pasta_mes_ano, tipo,
                         razao_social=None, run_id=None):
    try:
        wait.until(EC.visibility_of_element_located((By.XPATH, '/html/body/table/tbody/tr/td/table[2]')))

        # Clicar no filtro de período
        filtro_periodo = wait.until(EC.presence_of_element_located((By.XPATH, '//*[@id="rbt_filtro3"]')))
        filtro_periodo.click()

        # Preencher datas
        data_inicio_field = wait.until(EC.presence_of_element_located((By.ID, 'txtPeriodoInicial')))
        data_inicio_field.send_keys(Keys.HOME + data_inicio)

        data_fim_field = wait.until(EC.presence_of_element_located((By.ID, 'txtPeriodoFinal')))
        data_fim_field.send_keys(Keys.HOME + data_fim)

        # Tentar resolver o captcha até 3 vezes
        tentativas = 0
        while tentativas < 3:
            tentativas += 1
            if resolver_captcha(wait, driver, EC, By, logger,
                                run_id=run_id, empresa=razao_social, tipo=tipo,
                                tentativa=tentativas):
                break
            logger.warning(f"Tentativa {tentativas} de 3")
            time.sleep(2)
        else:
            diag.salvar_evidencia(driver, run_id, razao_social, tipo, "captcha", sufixo="esgotado")
            raise CaptchaFailedError(
                f"Falha ao resolver CAPTCHA após 3 tentativas para {razao_social}",
                empresa=razao_social,
            )

        btn_consultar = wait.until(EC.presence_of_element_located((By.ID, 'AplicarFiltro')))
        btn_consultar.click()
        diag.evento(run_id, razao_social, tipo, "submit_filtro", "ok")

        # Checagem explícita pós-submit: o site pode (a) mostrar 'lblConsultaVazia',
        # (b) avançar para a tela com 'btn_GerarPlanilha', ou (c) ficar travado no
        # form (captcha rejeitado ou erro silencioso). Esperamos até 10s por uma
        # dessas três condições.
        inicio_pos_submit = time.monotonic()
        timeout_pos_submit = 10
        resultado_pos_submit = None  # "vazio" | "ok" | "indefinido"

        while time.monotonic() - inicio_pos_submit < timeout_pos_submit:
            try:
                el_vazio = driver.find_element(By.ID, 'lblConsultaVazia')
                if el_vazio.is_displayed():
                    resultado_pos_submit = "vazio"
                    break
            except NoSuchElementException:
                pass
            try:
                el_ger = driver.find_element(By.ID, 'btn_GerarPlanilha')
                if el_ger.is_displayed():
                    resultado_pos_submit = "ok"
                    break
            except NoSuchElementException:
                pass
            time.sleep(0.5)
        else:
            resultado_pos_submit = "indefinido"

        diag.evento(run_id, razao_social, tipo, "pos_submit_filtro", "ok",
                    extras={"resultado": resultado_pos_submit,
                            "tempo_espera_ms": int((time.monotonic() - inicio_pos_submit) * 1000)})

        if resultado_pos_submit == "vazio":
            logger.warning("Consulta não retornou dados. Capturando a tela imediatamente.")
            screenshot_path = os.path.join(pasta_mes_ano, f'{tipo.upper()} SEM DADOS.png')
            driver.save_screenshot(screenshot_path)
            logger.info(f"Captura de tela salva em: {screenshot_path}")
            return False  # Consulta não retornou dados

        if resultado_pos_submit == "ok":
            logger.info("Consulta retornou dados. Prosseguindo com o download.")
            return True  # Consulta retornou dados

        # Indefinido: capturar evidência e marcar como suspeita de captcha rejeitado
        # ou bloqueio do site. Não dá pra decidir aqui — devolve True e deixa o
        # download tentar/timeoutar para registrarmos o sintoma real no diagnóstico.
        logger.warning("Estado pós-submit indefinido após 10s. Capturando evidência.")
        diag.salvar_evidencia(driver, run_id, razao_social, tipo, "pos_submit_filtro", sufixo="indefinido")
        return True

    except TimeoutException:
        logger.error("Erro ao preencher o formulário: elemento não encontrado")
        diag.salvar_evidencia(driver, run_id, razao_social, tipo, "preencher_formulario", sufixo="timeout")
        raise
    except BotPlanilhaError:
        raise
    except Exception as e:
        logger.error(f"Erro ao preencher o formulário: {e}")
        diag.salvar_evidencia(driver, run_id, razao_social, tipo, "preencher_formulario", sufixo="exception")
        raise

def baixar_planilha(driver, wait, logger, razao_social, tipo, download_dir, data_inicio,
                    run_id=None):
    try:
        logger.info("Clicando no botão para 'Gerar Planilha'")

        # Clicar no botão para gerar a planilha
        btn_gerar_planilha = wait.until(EC.presence_of_element_located((By.ID, 'btn_GerarPlanilha')))
        btn_gerar_planilha.click()
        diag.evento(run_id, razao_social, tipo, "gerar_planilha_click", "ok")

        timeout = int(os.getenv('DOWNLOAD_TIMEOUT', 60))
        start_time = time.time()
        viu_crdownload = False
        ultimo_snapshot = None

        while True:
            agora = time.time()
            decorrido = agora - start_time

            # Verifica se o tempo de espera foi excedido
            if decorrido > timeout:
                snapshot = _listar_diretorio_download(download_dir)
                diag.evento(run_id, razao_social, tipo, "aguardar_download", "timeout",
                            duracao_ms=int(decorrido * 1000),
                            extras={"timeout_s": timeout,
                                    "viu_crdownload": viu_crdownload,
                                    "snapshot_final": snapshot})
                diag.salvar_evidencia(driver, run_id, razao_social, tipo,
                                      "aguardar_download", sufixo="timeout")
                logger.error("Tempo de espera para download excedido")
                raise JobTimeoutError(
                    f"Timeout de download para {razao_social} (tipo {tipo}, limite {timeout}s)",
                    empresa=razao_social,
                )

            arquivos_baixados = os.listdir(download_dir)
            crdownloads = [a for a in arquivos_baixados if a.endswith('.crdownload')]
            finalizados = [a for a in arquivos_baixados if not a.endswith('.crdownload')]

            if crdownloads and not viu_crdownload:
                viu_crdownload = True
                diag.evento(run_id, razao_social, tipo, "aguardar_download", "iniciado",
                            extras={"crdownloads": crdownloads,
                                    "decorrido_s": round(decorrido, 1)})

            # Snapshot a cada ~5s para enxergar progresso
            chave_snapshot = (tuple(sorted(crdownloads)), tuple(sorted(finalizados)))
            if chave_snapshot != ultimo_snapshot and (int(decorrido) % 5 == 0):
                ultimo_snapshot = chave_snapshot

            # Só considera download concluído quando NÃO há .crdownload pendente
            # e existe pelo menos um arquivo finalizado.
            if finalizados and not crdownloads:
                arquivo_baixado = os.path.join(download_dir, finalizados[0])

                if not verificar_arquivo_em_uso(arquivo_baixado):
                    time.sleep(1)

                    tamanho = os.path.getsize(arquivo_baixado) if os.path.exists(arquivo_baixado) else 0

                    mes_ano_consulta = datetime.strptime(data_inicio, "%d/%m/%Y").strftime("%m%Y")
                    tipo_upper = tipo.upper()
                    razao_social_upper = razao_social.upper()
                    nome_arquivo_novo = f"{tipo_upper} {mes_ano_consulta} {razao_social_upper}.csv"
                    novo_caminho = os.path.join(download_dir, nome_arquivo_novo)

                    try:
                        os.rename(arquivo_baixado, novo_caminho)
                        diag.evento(run_id, razao_social, tipo, "aguardar_download", "ok",
                                    duracao_ms=int((time.time() - start_time) * 1000),
                                    extras={"arquivo_origem": finalizados[0],
                                            "arquivo_final": nome_arquivo_novo,
                                            "tamanho_bytes": tamanho})
                        logger.info(f"Arquivo renomeado para {nome_arquivo_novo} com sucesso.")
                        return novo_caminho
                    except FileNotFoundError:
                        logger.error(f"Arquivo não encontrado para renomeação: {arquivo_baixado}")
                        raise
                    except PermissionError:
                        logger.error(f"Erro de permissão ao renomear o arquivo {arquivo_baixado}. O arquivo está em uso.")
                        raise

            time.sleep(1)

    except BotPlanilhaError:
        raise
    except TimeoutException:
        logger.error("Erro ao baixar a planilha: tempo de espera excedido")
        raise
    except Exception as e:
        logger.error(f"Erro ao baixar a planilha: {e}")
        diag.evento(run_id, razao_social, tipo, "aguardar_download", "fail",
                    erro=f"{type(e).__name__}: {e}")
        diag.salvar_evidencia(driver, run_id, razao_social, tipo,
                              "aguardar_download", sufixo="exception")
        raise


def _listar_diretorio_download(download_dir):
    """Snapshot do diretório de download (nome + tamanho) para o diagnóstico."""
    try:
        itens = []
        for nome in os.listdir(download_dir):
            caminho = os.path.join(download_dir, nome)
            try:
                itens.append({"nome": nome, "bytes": os.path.getsize(caminho)})
            except OSError:
                itens.append({"nome": nome, "bytes": None})
        return itens
    except Exception:
        return []


def verificar_download(caminho_arquivo_baixado, timeout=10):
    """
    Função para verificar se o arquivo foi baixado corretamente.
    """
    tempo_inicial = time.time()
    while not os.path.exists(caminho_arquivo_baixado):
        time.sleep(1)
        if time.time() - tempo_inicial > timeout:
            raise FileNotFoundError(f"O arquivo {caminho_arquivo_baixado} não foi encontrado após {timeout} segundos.")

    if os.path.exists(caminho_arquivo_baixado):
        return True
    else:
        raise FileNotFoundError(f"O arquivo {caminho_arquivo_baixado} não foi encontrado.")

def verificar_arquivo_em_uso(arquivo):
    """Verifica se o arquivo está em uso por outro processo."""
    if not os.path.exists(arquivo):
        return True

    try:
        # Tenta abrir o arquivo para ver se ele está bloqueado
        with open(arquivo, 'r'):
            pass
        return False
    except Exception:
        return True
    
def _destino_base():
    # Lê DESTINO_BASE do env. Default: path Windows histórico (R:\FISCAL\...).
    # Em WSL/container, aponte pro mountpoint do share SRVDOC01\REDE\FISCAL.
    # Valida existência antes de criar subpastas, senão em Linux o makedirs
    # cria silenciosamente uma pasta "R:" no cwd quando o share não está montado.
    base = os.getenv("DESTINO_BASE", r"R:\FISCAL\00 PLANILHA SEFAZ")
    if not os.path.isdir(base):
        raise ShareUnavailableError(
            f"DESTINO_BASE não existe ou não é diretório: '{base}'. "
            "No Windows confira o mapeamento do R:. No WSL/container, "
            "monte o share \\\\SRVDOC01\\REDE no path apontado por DESTINO_BASE."
        )
    return base


def definir_pasta_mes_ano(razao_social, data_inicio):
    diretorio_raiz = _destino_base()
    razao_social_sanitizada = sanitizar_nome(razao_social)

    ano = datetime.strptime(data_inicio, "%d/%m/%Y").strftime("%Y")
    mes_ano = datetime.strptime(data_inicio, "%d/%m/%Y").strftime("%m%Y")
    pasta_empresa = os.path.join(diretorio_raiz, razao_social_sanitizada.upper())
    pasta_ano = os.path.join(pasta_empresa, ano)
    pasta_mes_ano = os.path.join(pasta_ano, mes_ano)

    os.makedirs(pasta_mes_ano, exist_ok=True)
    return pasta_mes_ano


def move_planilha(logger, razao_social, caminho_arquivo_baixado, data_inicio):
    try:
        diretorio_raiz = _destino_base()

        ano = datetime.strptime(data_inicio, "%d/%m/%Y").strftime("%Y")
        mes_ano = datetime.strptime(data_inicio, "%d/%m/%Y").strftime("%m%Y")
        pasta_empresa = os.path.join(diretorio_raiz, razao_social.upper())
        pasta_ano = os.path.join(pasta_empresa, ano)
        pasta_mes_ano = os.path.join(pasta_ano, mes_ano)

        try:
            os.makedirs(pasta_mes_ano, exist_ok=True)
        except OSError as e:
            raise ShareUnavailableError(
                f"Falha ao criar pasta destino '{pasta_mes_ano}' — share pode ter caído: {e}",
                empresa=razao_social,
            ) from e

        nome_arquivo = os.path.basename(caminho_arquivo_baixado)
        caminho_novo_arquivo = os.path.join(pasta_mes_ano, nome_arquivo)

        verificar_download(caminho_arquivo_baixado)

        try:
            shutil.move(caminho_arquivo_baixado, caminho_novo_arquivo)
        except OSError as e:
            raise ShareUnavailableError(
                f"Falha ao mover arquivo para '{caminho_novo_arquivo}' — share pode ter caído: {e}",
                empresa=razao_social,
            ) from e

        logger.info(f"Arquivo movido para: {caminho_novo_arquivo}")
    except BotPlanilhaError:
        raise
    except FileNotFoundError:
        logger.error("Arquivo não encontrado na pasta de downloads")
        raise
    except Exception as e:
        logger.error(f"Erro ao mover a planilha: {e}")
        raise
def excluir_diretorio(logger, diretorio):
    """
    Exclui o diretório e todo o seu conteúdo.
    """
    try:
        if os.path.exists(diretorio):
            shutil.rmtree(diretorio)
            logger.info(f"Diretório {diretorio} excluído com sucesso.")
    except Exception as e:
        logger.error(f"Erro ao excluir o diretório {diretorio}: {e}")

def download(logger, row, razao_social, login, senha, data_inicio, data_fim, dir_temp, tipo,
             driver=None, wait=None, run_id=None):
    """
    Realiza o download de arquivos do sistema SEFAZ.
    """
    download_dir = os.path.join('downloads', dir_temp)
    driver_instance = None
    razao_social_sanitizada = sanitizar_nome(razao_social)
    inicio_job = time.monotonic()

    diag.evento(run_id, razao_social_sanitizada, tipo, "job", "start",
                extras={"data_inicio": data_inicio, "data_fim": data_fim,
                        "dir_temp": dir_temp})

    try:
        os.makedirs(download_dir, exist_ok=True)

        if driver is None or wait is None:
            driver_instance, wait = configurar_driver(logger, download_dir)
        else:
             driver_instance = driver

        if tipo == "destinatario":
            abrir_sefaz_dest(driver_instance, logger, razao_social=razao_social_sanitizada)
            tipo_consulta = "DESTINATÁRIO"
        elif tipo == "remetente":
            abrir_sefaz_emit(driver_instance, logger, razao_social=razao_social_sanitizada)
            tipo_consulta = "REMETENTE"
        else:
            logger.error(f"Tipo de consulta inválido: {tipo}")
            raise ValueError(f"Tipo de consulta inválido: {tipo}")

        diag.evento(run_id, razao_social_sanitizada, tipo, "abrir_site", "ok")

        fazer_login(driver_instance, razao_social_sanitizada, wait, logger,
                    login, senha, run_id=run_id, tipo=tipo)

        pasta_mes_ano = definir_pasta_mes_ano(razao_social_sanitizada, data_inicio)
        retornou_dados = preencher_formulario(driver_instance, wait, logger,
                                              data_inicio, data_fim, pasta_mes_ano, tipo,
                                              razao_social=razao_social_sanitizada, run_id=run_id)
        if retornou_dados:
            caminho_arquivo_baixado = baixar_planilha(driver_instance, wait, logger,
                                                     razao_social_sanitizada, tipo_consulta,
                                                     download_dir, data_inicio, run_id=run_id)
            with diag.fase(run_id, razao_social_sanitizada, tipo, "mover_arquivo"):
                move_planilha(logger, razao_social_sanitizada, caminho_arquivo_baixado, data_inicio)
            diag.evento(run_id, razao_social_sanitizada, tipo, "job", "ok",
                        duracao_ms=int((time.monotonic() - inicio_job) * 1000))
            return "ok"
        else:
            logger.info(f"Nenhum dado encontrado para a consulta {tipo.upper()} da empresa {razao_social_sanitizada}.")
            diag.evento(run_id, razao_social_sanitizada, tipo, "job", "ok",
                        duracao_ms=int((time.monotonic() - inicio_job) * 1000),
                        extras={"resultado": "sem_dados"})
            return "no_data"

    except Exception as e:
        logger.error(f"Erro no processo de download para a empresa {razao_social_sanitizada} (tipo: {tipo}): {e}")
        if driver_instance:
            try:
                os.makedirs(download_dir, exist_ok=True)
                screenshot_path = os.path.join(download_dir, f'ERRO_{tipo.upper()}_{razao_social_sanitizada}.png')
                driver_instance.save_screenshot(screenshot_path)
                logger.info(f"Screenshot de erro salva em: {screenshot_path}")
            except Exception as sc_e:
                logger.error(f"Falha ao tirar screenshot do erro: {sc_e}")
        diag.evento(run_id, razao_social_sanitizada, tipo, "job", "fail",
                    duracao_ms=int((time.monotonic() - inicio_job) * 1000),
                    erro=f"{type(e).__name__}: {e}")
        diag.salvar_evidencia(driver_instance, run_id, razao_social_sanitizada, tipo, "job", sufixo="exception")
        raise
    finally:
        # Driver é gerenciado pela thread em main.py.
        logger.debug(f"Tentativa de download para {razao_social_sanitizada} (tipo: {tipo}) finalizada (com ou sem erro).")