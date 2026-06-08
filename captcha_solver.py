#captcha_solver.py
import os
import base64
import capsolver
from dotenv import load_dotenv
import shutil
from pathlib import Path
import time

import diagnostico as diag

# Carrega as variáveis de ambiente
load_dotenv()

# Configura a chave da API
capsolver.api_key = os.getenv('API_KEY')

def solve_image_captcha(image_path):
    # Converte a imagem para base64
    with open(image_path, "rb") as image_file:
        encoded_image = base64.b64encode(image_file.read()).decode("utf-8")
    
    # Cria e resolve a tarefa
    solution = capsolver.solve({
        "type": "ImageToTextTask",
        "module": "common",
        "body": encoded_image,
        "case": True,
        "score": 0.5 
    })
    
    return solution['text']

# Função para ver a imagem
def show_image(image_path):
    from PIL import Image
    img = Image.open(image_path)
    img.show()

def resolver_captcha(wait, driver, EC, By, logger,
                     run_id=None, empresa=None, tipo=None, tentativa=None):
    # Diretório temp único por thread/tentativa — evita corrida entre threads que
    # antes compartilhavam temp_captcha/ e podiam apagar imagem uma da outra.
    import uuid as _uuid
    temp_dir = os.path.join(os.getcwd(), 'temp_captcha', _uuid.uuid4().hex[:8])
    os.makedirs(temp_dir, exist_ok=True)
    inicio = time.monotonic()

    try:
        # Capturar elemento do CAPTCHA
        captcha_img = wait.until(EC.presence_of_element_located((By.ID, 'img_captcha')))

        # Obter conteúdo da imagem
        src = captcha_img.get_attribute('src')
        logger.debug(f"SRC do CAPTCHA: {src[:50]}...")

        if 'base64' in src:
            img_base64 = src.split(',', 1)[1]
        else:
            from selenium.webdriver.remote.file_detector import LocalFileDetector
            driver.file_detector = LocalFileDetector()
            img_data = captcha_img.screenshot_as_png
            img_base64 = base64.b64encode(img_data).decode('utf-8')

        captcha_path = os.path.join(temp_dir, 'captcha_temp.png')
        with open(captcha_path, 'wb') as f:
            f.write(base64.b64decode(img_base64))

        captcha_text = solve_image_captcha(captcha_path)
        # Não logar o texto do CAPTCHA em claro (ia pro log diário E pro
        # diagnostico-*.jsonl). Só o tamanho — suficiente pra diagnóstico.
        logger.debug(f"CAPTCHA resolvido ({len(captcha_text)} chars).")

        campo = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, '#txt_cod_antirobo')))
        campo.clear()
        campo.send_keys(captcha_text)

        diag.evento(run_id, empresa, tipo, "captcha", "ok",
                    tentativa=tentativa,
                    duracao_ms=int((time.monotonic() - inicio) * 1000),
                    extras={"captcha_len": len(captcha_text),
                            "src_tipo": "base64" if 'base64' in src else "screenshot"})
        return True

    except IndexError as e:
        logger.error(f"Erro de formato do CAPTCHA: {str(e)}")
        diag.evento(run_id, empresa, tipo, "captcha", "fail",
                    tentativa=tentativa,
                    duracao_ms=int((time.monotonic() - inicio) * 1000),
                    erro=f"IndexError: {e}")
        return False
    except Exception as e:
        logger.error(f"Erro geral ao resolver CAPTCHA: {str(e)}")
        diag.evento(run_id, empresa, tipo, "captcha", "fail",
                    tentativa=tentativa,
                    duracao_ms=int((time.monotonic() - inicio) * 1000),
                    erro=f"{type(e).__name__}: {e}")
        return False
    finally:
        # Limpa só o subdiretório próprio (não mexe nos das outras threads)
        shutil.rmtree(temp_dir, ignore_errors=True)
        time.sleep(1)