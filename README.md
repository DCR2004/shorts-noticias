# Shorts de notícias automáticos (Brasil)

Este sistema corre sozinho no GitHub, **25 vezes por dia**, e em cada execução:

1. Lê as últimas notícias de 12 jornais brasileiros (g1, Folha, CNN Brasil, Agência Brasil, BBC Brasil, Poder360, Revista Oeste, UOL, Metrópoles, CartaCapital, Gazeta do Povo, InfoMoney).
2. Junta as notícias que falam do mesmo assunto e dá prioridade às que aparecem em vários jornais.
3. Pede ao **Google Gemini** para escolher o assunto mais relevante e escrever um roteiro original de 45 a 55 segundos, neutro e com as fontes citadas.
4. Gera a narração com uma voz neural brasileira gratuita e legendas grandes, palavra a palavra.
5. Monta o vídeo vertical (1080x1920) com fotos ilustrativas do Pexels, a manchete e as fontes.
6. Envia o vídeo para o teu canal com título, descrição, links das fontes e hashtags.
7. Guarda um histórico para não repetir assuntos.

Configuras tudo **uma vez** (cerca de 30 a 40 minutos). Tudo o que é usado tem plano gratuito.

---

## O que vais precisar

- A conta Google que é dona do canal do YouTube
- Uma conta no GitHub (gratuita): https://github.com/signup
- Uma chave do Gemini (passo 1)
- Uma chave do Pexels (passo 2; sem ela, o vídeo usa fundos coloridos em vez de fotos)

---

## Passo 1: chave do Gemini (a IA que escreve)

1. Abre https://aistudio.google.com/apikey e entra com a tua conta Google.
2. Clica em **Create API key** e copia a chave. Guarda-a num bloco de notas.

## Passo 2: chave do Pexels (fotos gratuitas)

1. Abre https://www.pexels.com/api/ e cria conta ou entra.
2. Pede a chave (**Your API Key**) e copia-a.

## Passo 3: autorizar o envio para o YouTube (Google Cloud)

1. Abre https://console.cloud.google.com e cria um **projeto novo** (por exemplo "shorts-noticias").
2. Menu ☰, depois **APIs e serviços**, depois **Biblioteca**. Procura **YouTube Data API v3** e clica em **Ativar**.
3. Menu ☰, depois **APIs e serviços**, depois **Ecrã de consentimento OAuth** (pode aparecer como **Google Auth Platform**). Clica em **Começar**:
   - Nome da app: "Shorts Noticias"; email: o teu
   - Público: **Externo**
   - Contacto: o teu email. Depois clica em **Criar**.
4. ⚠️ **Muito importante:** em **Público** (*Audience*), clica em **Publicar app** e confirma, até o estado ficar **Em produção**.
   Se ficar em modo "Teste", a autorização expira a cada 7 dias e os envios param.
5. Em **Clientes** (*Clients*), clica em **Criar cliente**:
   - Tipo: **Aplicação Web**
   - Em **URIs de redirecionamento autorizados**, adiciona: "https://developers.google.com/oauthplayground"
   - Clica em **Criar** e copia o **ID do cliente** e o **Segredo do cliente**.

## Passo 4: obter o "refresh token" (a autorização permanente)

1. Abre https://developers.google.com/oauthplayground
2. Clica na roda dentada ⚙ (canto superior direito):
   - Marca **Use your own OAuth credentials**
   - Cola o **ID do cliente** e o **Segredo do cliente**
   - Se aparecer a opção *Access type*, escolhe **Offline**.
3. À esquerda, na caixa **Input your own scopes**, escreve:
   "https://www.googleapis.com/auth/youtube.upload"
   e clica em **Authorize APIs**.
4. Entra com a conta Google **do canal**. Se o canal for uma conta de marca, escolhe o canal.
   Vai aparecer "A Google não validou esta app". É normal, porque a app é tua: clica em **Avançadas**, depois em **Aceder a Shorts Noticias**, e por fim em **Continuar**.
5. Clica em **Exchange authorization code for tokens** e copia o **Refresh token**.

> Alternativa para quem tem Python no computador: "python shorts.py --obter-token client_secret.json" (com um cliente OAuth do tipo "App para computador").

## Passo 5: pôr o sistema no GitHub

1. No GitHub, clica em **New repository**. Nome: "shorts-noticias", marca **Private** e clica em **Create repository**.
2. Clica em **uploading an existing file** e arrasta **todo o conteúdo** da pasta descompactada, incluindo a pasta ".github". Depois clica em **Commit changes**.
   - No Mac, a pasta ".github" está escondida. No Finder, carrega em **Cmd + Shift + .** para a ver.
   - Confirma que o repositório tem o ficheiro ".github/workflows/shorts.yml".
3. Vai a **Settings**, depois **Secrets and variables**, depois **Actions**, e cria 5 segredos com **New repository secret**:

   | Nome (exatamente assim) | Valor |
   |---|---|
   | "GEMINI_API_KEY" | chave do passo 1 |
   | "PEXELS_API_KEY" | chave do passo 2 |
   | "YT_CLIENT_ID" | ID do cliente (passo 3.5) |
   | "YT_CLIENT_SECRET" | Segredo do cliente (passo 3.5) |
   | "YT_REFRESH_TOKEN" | Refresh token (passo 4.5) |

4. Vai a **Settings**, depois **Actions**, depois **General**. Em **Workflow permissions**, escolhe **Read and write permissions** e clica em **Save**.

## Passo 6: testar

1. Abre o separador **Actions**. Se pedir, ativa os workflows. Depois escolhe **Shorts de notícias** e clica em **Run workflow**.
   Deixa **"Publicar no YouTube?" desmarcado** e clica em **Run workflow**.
2. Espera 3 a 6 minutos e abre a execução:
   - Em **Summary** vês o roteiro e as fontes.
   - Em **Artifacts** descarregas o vídeo ("short-N") para o veres.
3. Se estiver tudo bem, corre outra vez com **Publicar** marcado. O vídeo aparece no YouTube Studio como **agendado** e fica público 1 hora depois.

**Pronto.** A partir daqui o sistema corre sozinho todos os dias, a cada 45 minutos, **entre as 06h e a meia-noite de Brasília** (cada vídeo fica público 1 hora depois de ser criado).

---

## Personalizar ("config.yaml")

Podes editar no próprio GitHub: abre o ficheiro e clica no lápis ✏️.

- **canal.nome**: o nome que aparece no topo do vídeo.
- **voz.nome**: "pt-BR-AntonioNeural" (masculina) ou "pt-BR-FranciscaNeural" (feminina).
- **youtube.modo**:
  - "agendado" (recomendado): o vídeo fica público X horas depois. Se algo estiver errado, apagas no YouTube Studio antes disso (a app de telemóvel serve).
  - "publico": publica de imediato, sem revisão.
  - "privado": és tu que publicas cada vídeo.
- **noticias.fontes**: acrescentar ou retirar jornais (endereço RSS).
- **noticias.evitar**: temas que a IA nunca escolhe.
- **Horários**: em ".github/workflows/shorts.yml" (linhas "cron", em hora UTC; Brasília = UTC−3).

---

## Notas importantes

- **A IA pode errar.** O roteiro só usa os textos dos jornais e cita as fontes, mas convém ver os vídeos nas primeiras semanas. O canal e o que publica são da tua responsabilidade.
- **Monetização:** o YouTube pode recusar monetizar canais 100% automáticos e repetitivos ("conteúdo inautêntico"). Ajuda rever os vídeos, variar o formato e, de vez em quando, publicar algo teu.
- **Transparência:** cada vídeo diz "Narração gerada por IA · Imagens ilustrativas" e a descrição tem os links das fontes.
- **Custos (gratuito dentro dos limites):**
  - GitHub Actions: um repositório privado tem 2000 minutos/mês grátis e isto usa cerca de 500 a 900.
  - Gemini: tem plano gratuito, mas os limites podem mudar. Se a quota esgotar, o sistema tenta o modelo seguinte da lista.
  - Pexels e a voz do Edge: grátis.
- O GitHub às vezes atrasa as execuções agendadas alguns minutos. É normal.

## Problemas comuns

| Mensagem no log | Solução |
|---|---|
| "Falta o segredo GEMINI_API_KEY" / "Faltam os segredos do YouTube" | Confirma os nomes dos segredos (passo 5.3), letra a letra. |
| "invalid_grant" | A autorização do YouTube expirou: a app ficou em modo "Teste" (passo 3.4) ou mudaste a password. Repete o passo 4 e atualiza "YT_REFRESH_TOKEN". |
| "Gemini ... HTTP 429" | Quota gratuita esgotada. Espera umas horas ou ativa a faturação no Google AI Studio. |
| "✗ NomeDoJornal" | Esse jornal mudou o endereço RSS. Corrige-o ou apaga-o no "config.yaml". |
| Falha no passo "Atualizar histórico" | Ativa **Read and write permissions** (passo 5.4). |
| Vídeo com fundo colorido em vez de fotos | Falta o segredo "PEXELS_API_KEY". |

## Estrutura

    shorts.py                    todo o sistema (notícias → roteiro → voz → vídeo → YouTube)
    config.yaml                  configuração do canal
    requirements.txt             bibliotecas Python
    .github/workflows/shorts.yml horários e execução automática
    data/historico.json          histórico (criado e atualizado sozinho)

A fonte Montserrat (licença OFL) é descarregada automaticamente em cada execução.

Teste local sem internet: "python shorts.py --teste-offline"
