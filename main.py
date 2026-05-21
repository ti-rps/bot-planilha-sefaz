# main.py — entry point GUI (Tkinter). Backend de execução vive em runner.py.
import os
import uuid
import time
import shutil
import logging
import threading
import pandas as pd
import tkinter as tk
import ler_planilha as lp
import diagnostico as diag
import runner
import queue

from dotenv import load_dotenv
from tkinter import ttk
from tkinter import messagebox
from tkcalendar import DateEntry
from gspread.exceptions import APIError

load_dotenv()

# Fila para comunicação entre threads de trabalho e a thread da GUI
gui_event_queue = queue.Queue()
logger = None # Será configurado em configurar_logger e atribuído no __main__
df = None # DataFrame global
treeview = None # Treeview global
root = None # Janela principal global

def configurar_logger():
    global logger # Para garantir que estamos usando a instância correta
    data_hoje = time.strftime("%d-%m-%Y")
    if not os.path.exists('log'):
        os.makedirs('log')
    
    # Remove handlers existentes para evitar duplicação se a função for chamada múltiplas vezes
    # No entanto, como logger é configurado uma vez no __main__, não é um grande problema.
    # if logging.getLogger(__name__).hasHandlers():
    # logging.getLogger(__name__).handlers.clear()

    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(f"log/{data_hoje}.log", encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    logger = logging.getLogger(__name__) # Pega o logger configurado pelo basicConfig
    return logger 

def configurar_estilo():
    estilo = ttk.Style()
    estilo.theme_use('clam')

    estilo.configure('Treeview',
                     background='#EAEAEA',
                     foreground='black',
                     rowheight=25,
                     fieldbackground='#EAEAEA',
                     font=('Arial', 10))
    estilo.map('Treeview', background=[('selected', '#A9CCE3')])

    estilo.configure('TButton',
                     font=('Arial', 10, 'bold'),
                     background='#87CEFA',
                     foreground='white',
                     borderwidth=0,
                     relief='flat')
    estilo.map('TButton', background=[('!disabled', '#366ec2')], foreground=[('!disabled', 'white')])

    estilo.configure('Title.TLabel',
                     font=('Arial', 16, 'bold'),
                     foreground='black',
                     background='#F0F0F0')

    estilo.configure('TLabel', font=('Arial', 12), background='#F0F0F0')
    estilo.configure('TEntry', font=('Arial', 12))

# Funções de processamento por empresa foram movidas pra runner.py (FASE 3).
# Esta GUI agora orquestra via runner.run_batch.


def carregar_empresas(dataframe_local, sort_column=None, reverse=False):
    global treeview
    if treeview is None:
        # logger pode não estar configurado se isso for chamado antes de __main__
        # print("ERRO: Treeview não inicializada antes de carregar empresas.")
        if logger: logger.error("Treeview não inicializada antes de carregar empresas.")
        return

    for item in treeview.get_children():
        treeview.delete(item)

    if dataframe_local is None:
        if logger: logger.warning("DataFrame para carregar empresas está Nulo.")
        return

    if sort_column and sort_column in dataframe_local.columns:
        try:
            dataframe_local = dataframe_local.sort_values(by=sort_column, ascending=not reverse)
        except Exception as e_sort:
            if logger: logger.error(f"Erro ao ordenar por coluna '{sort_column}': {e_sort}")
            # Prossegue sem ordenação se falhar

    for index, row in dataframe_local.iterrows():
        codigo = row.get('Código', '')
        razao_social_val = row.get('RAZÃO SOCIAL', '')
        cnpj = row.get('CNPJ', '')
        # Garantir que 'Senha Robô' seja string para .strip()
        senha_robo_val = str(row.get('Senha Robô', '')) 
        status_text = "Disponível" if pd.notna(row.get('Senha Robô')) and senha_robo_val.strip() != "" else "Indisponível"
        contribuinte = "Sim" if row.get('Contribuinte') == 'S' else "Não"
        status_tag = "indisponivel" if status_text == "Indisponível" else "disponivel"
        
        treeview.insert("", tk.END,
                        values=(codigo, razao_social_val, cnpj, status_text, contribuinte),
                        tags=(status_tag,))

def ordenar_por_coluna(tv, coluna, reverse):
    # Tenta converter para numérico se possível para melhor ordenação, senão string
    all_data = []
    for k in tv.get_children(''):
        val = tv.set(k, coluna)
        try:
            # Tenta converter para float se for um número (int ou float)
            # Isso ajuda a ordenar "10" depois de "2"
            num_val = float(val)
            all_data.append((num_val, k))
        except (ValueError, TypeError):
            all_data.append((val, k)) # Mantém como string se não for conversível

    all_data.sort(key=lambda item: item[0], reverse=reverse)

    for index, (val_sorted, k_sorted) in enumerate(all_data):
        tv.move(k_sorted, '', index)
    
    tv.heading(coluna, command=lambda c=coluna: ordenar_por_coluna(tv, c, not reverse))


def mostrar_menu():
    global treeview, df, root 

    root = tk.Tk()
    root.title("Menu de Consultas")
    root.geometry("1200x700")
    root.configure(bg='#F0F0F0')

    configurar_estilo()

    def processar_gui_eventos():
        try:
            while True:
                tipo_evento, args_evento = gui_event_queue.get_nowait()
                if tipo_evento == "show_warning":
                    titulo, mensagem = args_evento
                    messagebox.showwarning(titulo, mensagem)
                elif tipo_evento == "show_error":
                    titulo, mensagem = args_evento
                    messagebox.showerror(titulo, mensagem)
                elif tipo_evento == "show_info":
                    titulo, mensagem = args_evento
                    messagebox.showinfo(titulo, mensagem)
        except queue.Empty:
            pass
        root.after(100, processar_gui_eventos)

    label = ttk.Label(root, text="AUTOMAÇÃO PLANILHA SEFAZ", style='Title.TLabel')
    label.pack(pady=40)

    data_frame_ui = tk.Frame(root, bg='#F0F0F0')
    data_frame_ui.pack(pady=5)

    data_inicial_label = ttk.Label(data_frame_ui, text="Data Inicial:", style='TLabel')
    data_inicial_label.grid(row=0, column=0, padx=5)
    data_inicial_entry = DateEntry(data_frame_ui, date_pattern='dd/mm/yyyy', locale='pt_BR')
    data_inicial_entry.grid(row=0, column=1, padx=5)

    data_final_label = ttk.Label(data_frame_ui, text="Data Final:", style='TLabel')
    data_final_label.grid(row=0, column=2, padx=5)
    data_final_entry = DateEntry(data_frame_ui, date_pattern='dd/mm/yyyy', locale='pt_BR')
    data_final_entry.grid(row=0, column=3, padx=5)

    search_frame = tk.Frame(root, bg='#F0F0F0')
    search_frame.pack(pady=10)

    search_var = tk.StringVar()
    search_entry = ttk.Entry(search_frame, textvariable=search_var, width=50, font=('Arial', 12), foreground='grey')
    search_entry.grid(row=0, column=0, padx=5, ipady=2)
    search_entry.insert(0, "Pesquisar...")

    def on_search_focus_in(event):
        if search_var.get() == "Pesquisar...":
            search_entry.delete(0, tk.END)
            search_entry.config(foreground='black')

    def on_search_focus_out(event):
        if not search_var.get().strip(): # Se estiver vazio ou só espaços
            search_entry.delete(0, tk.END)
            search_entry.insert(0, "Pesquisar...")
            search_entry.config(foreground='grey')
    
    search_entry.bind("<FocusIn>", on_search_focus_in)
    search_entry.bind("<FocusOut>", on_search_focus_out)

    def pesquisar_empresas_gui():
        termo_pesquisa = search_var.get().lower()
        if termo_pesquisa and termo_pesquisa != "pesquisar...":
            if df is not None:
                df_filtrado = df[df['RAZÃO SOCIAL'].astype(str).str.lower().str.contains(termo_pesquisa, na=False)]
                carregar_empresas(df_filtrado)
            else:
                gui_event_queue.put(("show_error", ("Erro", "DataFrame de empresas não carregado.")))
        else:
            carregar_empresas(df)

    def limpar_pesquisa_gui():
        search_var.set("")
        search_entry.delete(0, tk.END)
        search_entry.insert(0, "Pesquisar...")
        search_entry.config(foreground='grey')
        if df is not None: carregar_empresas(df)
        root.focus_set()

    btn_pesquisar = ttk.Button(search_frame, text="Pesquisar", command=pesquisar_empresas_gui, width=15)
    btn_pesquisar.grid(row=0, column=1, padx=5)

    btn_limpar = ttk.Button(search_frame, text="Limpar Pesquisa", command=limpar_pesquisa_gui, width=15)
    btn_limpar.grid(row=0, column=2, padx=5)

    def selecionar_todas_empresas_gui():
        if treeview:
            for item in treeview.get_children():
                treeview.selection_add(item)

    btn_selecionar_todas = ttk.Button(search_frame, text="Selecionar tudo", command=selecionar_todas_empresas_gui, width=15)
    btn_selecionar_todas.grid(row=0, column=3, padx=5)

    treeview_frame = tk.Frame(root)
    treeview_frame.pack(pady=5, fill=tk.BOTH, expand=True)

    columns = ("Código", "RAZÃO SOCIAL", "CNPJ", "Status", "Contribuinte")
    treeview = ttk.Treeview(treeview_frame, columns=columns, show='headings', selectmode='extended')
    
    for col_name in columns:
        treeview.heading(col_name, text=col_name, command=lambda c=col_name: ordenar_por_coluna(treeview, c, False))
        width = 100
        if col_name == "RAZÃO SOCIAL": width = 350
        elif col_name == "CNPJ": width = 150
        elif col_name == "Status": width = 100
        elif col_name == "Código": width = 80
        treeview.column(col_name, width=width, anchor='w' if col_name == "RAZÃO SOCIAL" else 'center')


    scrollbar = ttk.Scrollbar(treeview_frame, orient="vertical", command=treeview.yview)
    treeview.configure(yscroll=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    treeview.pack(side="left", fill="both", expand=True)

    treeview.tag_configure('indisponivel', foreground='red', font=('Arial', 10, 'bold'))
    treeview.tag_configure('disponivel', foreground='black', font=('Arial', 10))

    if df is not None:
        carregar_empresas(df)
    else:
        if logger: logger.warning("DataFrame 'df' é None ao tentar carregar empresas inicialmente.")
        gui_event_queue.put(("show_error", ("Erro de Dados", "Planilha de empresas não carregada.")))


    chk_frame = tk.Frame(root, bg='#F0F0F0')
    chk_frame.pack(pady=10)

    chk_destinatario_var = tk.BooleanVar()
    chk_remetente_var = tk.BooleanVar()

    chk_destinatario = ttk.Checkbutton(chk_frame, text="Destinatário", variable=chk_destinatario_var)
    chk_remetente = ttk.Checkbutton(chk_frame, text="Remetente", variable=chk_remetente_var)

    chk_destinatario.grid(row=0, column=0, padx=5)
    chk_remetente.grid(row=0, column=1, padx=5)

    button_frame = tk.Frame(root, bg='#F0F0F0')
    button_frame.pack(pady=10)

    progress = ttk.Progressbar(root, orient='horizontal', length=400, mode='determinate')
    progress.pack(pady=20)

    def validar_datas_gui():
        try:
            data_inicial_val = data_inicial_entry.get_date()
            data_final_val = data_final_entry.get_date()
            if data_inicial_val > data_final_val:
                raise ValueError("A data inicial não pode ser maior que a data final.")
            return True
        except ValueError as ve: # Captura o erro de get_date se a data for inválida
            messagebox.showerror("Erro de Data", f"Data inválida: {ve}")
            return False
    
    def limpar_diretorio_downloads_temporarios():
        download_root_dir = 'downloads'
        if os.path.exists(download_root_dir):
            for item_name in os.listdir(download_root_dir):
                item_path = os.path.join(download_root_dir, item_name)
                # Tenta remover apenas se for um diretório (as pastas UUID)
                if os.path.isdir(item_path):
                    try:
                        # Verifica se o nome da pasta parece um UUID (opcional, para segurança)
                        uuid.UUID(item_name, version=4)
                        shutil.rmtree(item_path)
                        if logger: logger.info(f"Diretório temporário {item_path} limpo.")
                    except ValueError: # Não é um UUID válido, talvez não deva ser apagado automaticamente
                        if logger: logger.warning(f"Item {item_path} não parece ser um diretório UUID, não foi apagado.")
                    except Exception as e_clean_item:
                        if logger: logger.error(f"Erro ao limpar subdiretório {item_path}: {e_clean_item}")
        else:
            os.makedirs(download_root_dir) # Cria o diretório 'downloads' se não existir

    def iniciar_processamento_parallel_gui():
        if not validar_datas_gui():
            return

        data_inicial_str = data_inicial_entry.get_date().strftime("%d/%m/%Y")
        data_final_str = data_final_entry.get_date().strftime("%d/%m/%Y")
        selecionadas_itens_ids = treeview.selection()

        destinatario_bool = chk_destinatario_var.get()
        remetente_bool = chk_remetente_var.get()

        if not selecionadas_itens_ids:
            messagebox.showerror("Erro", "Nenhuma loja foi selecionada.")
            return
        if not destinatario_bool and not remetente_bool:
            messagebox.showerror("Erro", "Você deve selecionar Destinatário, Remetente ou ambos.")
            return

        empresas_para_processar_list = [treeview.item(item_id, 'values') for item_id in selecionadas_itens_ids]

        # Diagnóstico: run_id único por clique em Consultar; MAX_WORKERS via .env
        try:
            max_workers = max(1, int(os.getenv("MAX_WORKERS", "3")))
        except ValueError:
            max_workers = 3
        run_id = diag.gerar_run_id()
        total_empresas = len(empresas_para_processar_list)
        logger.info(f"[diag] Iniciando run_id={run_id} | MAX_WORKERS={max_workers} | empresas={total_empresas}")
        diag.evento(run_id, None, None, "batch", "start",
                    extras={"max_workers": max_workers,
                            "total_empresas": total_empresas,
                            "data_inicial": data_inicial_str,
                            "data_final": data_final_str,
                            "destinatario": destinatario_bool,
                            "remetente": remetente_bool})

        progress['value'] = 0
        progress['maximum'] = total_empresas
        if root: root.update_idletasks()

        if btn_consultar: btn_consultar.config(state=tk.DISABLED)

        def update_progress(processed, _total):
            # Callback do runner é chamado em thread worker — agenda no main loop.
            if root: root.after(0, lambda p=processed: progress.configure(value=p))

        def worker():
            summary, status, error_class, partial_success = runner.run_batch(
                empresas_para_processar_list,
                data_inicial_str, data_final_str,
                destinatario_bool, remetente_bool,
                df, logger,
                max_workers=max_workers,
                run_id=run_id,
                progress_callback=update_progress,
            )

            def on_done():
                limpar_diretorio_downloads_temporarios()
                try:
                    relatorio_path = diag.gerar_relatorio(run_id, max_workers, total_empresas)
                except Exception as e_rel:
                    logger.error(f"Falha ao gerar relatório do run {run_id}: {e_rel}")
                    relatorio_path = None

                diag.evento(run_id, None, None, "batch", "end",
                            extras={"status": status,
                                    "error_class": error_class,
                                    "partial_success": partial_success,
                                    "ok": len(summary["ok"]),
                                    "no_data": len(summary["no_data"]),
                                    "failed": len(summary["failed"])})
                logger.info(f"[diag] run_id={run_id} status={status} error_class={error_class} partial={partial_success}")

                messagebox.showinfo(
                    "Processo Concluído",
                    runner.formatar_mensagem_summary(summary, status, error_class, partial_success, relatorio_path),
                )

                progress['value'] = 0
                if btn_consultar: btn_consultar.config(state=tk.NORMAL)

            if root: root.after(0, on_done)

        threading.Thread(target=worker, daemon=True).start()

    btn_consultar = ttk.Button(button_frame, text="Consultar", command=iniciar_processamento_parallel_gui, width=20)
    btn_consultar.grid(row=0, column=1, padx=10)

    processar_gui_eventos() # Inicia o loop de verificação da fila de eventos da GUI
    
    root.mainloop()

if __name__ == "__main__":
    # logger é configurado primeiro
    logger = configurar_logger()
    
    try:
        df = lp.get_df(logger)
        if df is None or df.empty:
            if logger: logger.error("DataFrame não pôde ser carregado ou está vazio. Encerrando aplicação.")
            # Cria uma root temporária só para mostrar o erro antes de sair
            temp_root_error = tk.Tk()
            temp_root_error.withdraw() 
            messagebox.showerror("Erro Crítico", "Erro ao carregar a planilha de empresas. A aplicação será encerrada.")
            temp_root_error.destroy()
            exit()
    except APIError as api_err:
        if logger: logger.error(f"Erro de API do Google Sheets ao carregar a planilha: {api_err}", exc_info=True)
        temp_root_error = tk.Tk()
        temp_root_error.withdraw()
        messagebox.showerror("Erro Crítico", f"Erro de API ao carregar planilha: {api_err}\nVerifique suas credenciais e conexão. A aplicação será encerrada.")
        temp_root_error.destroy()
        exit()
    except Exception as e:
        if logger: logger.error(f"Erro desconhecido ao carregar a planilha: {e}", exc_info=True)
        temp_root_error = tk.Tk()
        temp_root_error.withdraw()
        messagebox.showerror("Erro Crítico", f"Erro desconhecido ao carregar planilha: {e}. A aplicação será encerrada.")
        temp_root_error.destroy()
        exit()
        
    mostrar_menu()