#ler_planilha.py
import os
import gspread
import pandas as pd

from dotenv import load_dotenv
from gspread.exceptions import APIError
from oauth2client.service_account import ServiceAccountCredentials

def get_df(logger):
    try:
        # Carregar variáveis de ambiente
        load_dotenv()

        # Obter o URL da planilha
        google_sheet_url = os.getenv("GOOGLE_SHEET_URL")
        if google_sheet_url is None:
            logger.error("A variável de ambiente GOOGLE_SHEET_URL não está definida.")
            raise ValueError("A URL da planilha não está definida. Verifique o arquivo .env")

        # Definir o escopo
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

        # Path da credencial via env (GOOGLE_CREDENTIALS_FILE). Default mantém
        # o comportamento atual; em container monta-se a credencial como volume
        # e aponta GOOGLE_CREDENTIALS_FILE pra esse mountpoint.
        credentials_file = os.getenv(
            "GOOGLE_CREDENTIALS_FILE",
            "credentials/citric-nimbus-436114-g8-daacef9f0900.json",
        )
        if not os.path.isfile(credentials_file):
            logger.error(f"Arquivo de credenciais não encontrado: {credentials_file}")
            raise FileNotFoundError(f"Credenciais Google não encontradas em '{credentials_file}'.")

        creds = ServiceAccountCredentials.from_json_keyfile_name(credentials_file, scope)
        client = gspread.authorize(creds)

        # Abrir a planilha pelo URL
        spreadsheet = client.open_by_url(google_sheet_url)

        # Selecionar a primeira aba da planilha
        sheet = spreadsheet.sheet1

        # Obter todos os valores da planilha
        data = sheet.get_all_records()

        # Converter os dados para um DataFrame do pandas
        df = pd.DataFrame(data)

        return df

    except gspread.exceptions.APIError as e:
        logger.error(f"Erro na API do Google Sheets: {e}")
        raise
    except Exception as e:
        logger.error(f"Erro ao obter o DataFrame da planilha: {e}")
        raise

def get_empresa(logger, row):
    logger.info(f"Empresa: {row['RAZÃO SOCIAL']}")
    empresa = row['RAZÃO SOCIAL']
    return empresa

def get_login(logger, row):
    logger.info(f"Login: {row['Login']}")
    login = row['Login']
    return login

def get_senha(logger, row):
    logger.info(f"Senha: {row['Senha Robô']}")
    senha = row['Senha Robô']
    return senha

def get_isContribuinte(logger, row):
    logger.info(f"Senha: {row['Contribuinte']}")
    isContribuinte = row['Senha Robô']
    return isContribuinte