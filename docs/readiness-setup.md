# Gate de prontidão e dispatch ODIM

Esta alteração é de código e operação. Não altera o manuscrito nem demonstra ganhos de desempenho novos. Os testes usam fixtures sintéticas, executáveis falsos e HTTP simulado; não fazem deploy no laboratório.

## Critério de prontidão

O provisionamento Terraform e a obtenção de um IP não significam que o VIM já pode receber o Free5GC. Antes do handoff, a Pipeline 1 verifica no endpoint selecionado:

1. Acesso SSH através do proxy já configurado.
2. API Kubernetes `/readyz` acessível e respondendo corretamente.
3. Quantidade mínima configurada de nós, todos os nós registrados em `Ready=True` e pelo menos um nó pronto não marcado como unschedulable.
4. Três observações consecutivas de prontidão. A consulta padrão ocorre a cada 5 segundos; qualquer observação ruim reinicia a contagem.

O prazo padrão é 900 segundos, contado com relógio monotônico. Uma consulta SSH tem prazo limitado; seus processos locais são encerrados em caso de timeout. Timeout produz código 124 e não libera dispatch. Configuração inválida também falha de forma fechada.

A Pipeline 2 repete o gate antes de criar recursos no OSM. Isso reduz a janela entre a primeira observação e o deploy, mas não garante disponibilidade permanente após a observação. A prontidão do cluster não é prova de registro de UEs, sessão PDU ou tráfego 5G.

## Ativação segura

- O novo código está em uma branch de revisão. Nenhuma infraestrutura foi alterada para validá-lo.
- `dispatch_free5gc` tem default `false`, para não duplicar o dispatcher externo existente.
- Primeiro disponibilize a versão de P2 que contém os novos inputs e o novo gate. Depois configure P1 com `free5gc_ref` apontando para essa versão.
- Somente após revisar a configuração e desativar o disparo duplicado no controlador externo, habilite `dispatch_free5gc=true`.
- Uma mudança no fork Terraform não atualiza automaticamente o repositório original da organização onde foram localizados os históricos anteriores. A escolha do repositório de execução precisa ser explícita.

## Segredos de CI

Os segredos de proxy, host QEMU e OSM existentes continuam sendo necessários. Adicionalmente:

| Uso | Configuração |
|---|---|
| Gate SSH em P1 e P2 | `CLUSTER_PASSWORD` ou `CLUSTER_SSH_KEY` em cada repositório que executa a respectiva pipeline |
| Usuário da imagem | `cluster_user`, default `packer`; sudo das consultas precisa funcionar sem prompt |
| Dispatch automático P1 para P2 | `FREE5GC_DISPATCH_TOKEN`, autorizado a criar workflow_dispatch no repositório alvo |

Prefira um token dedicado ou uma instalação GitHub App com o menor acesso necessário. Para token fine-grained, a criação do dispatch requer permissão Actions de escrita no alvo. O token pessoal utilizado pelo agente para commits não foi copiado para secrets de CI. O GITHUB_TOKEN do repositório de origem não deve ser presumido autorizado a outro repositório.

A chave opcional é escrita apenas no runner e com permissão restrita. O gate passa a senha via SSHPASS, não como argumento de processo. O kubeconfig é obtido em JSON, mantido com modo 0600 e não é publicado nos artifacts de métricas.

### Confiança TLS do novo monitor

O monitor NS valida o certificado HTTPS por padrão, usando a CA confiável do host remoto osm. Para uma CA privada, configure a variável de repositório `OSM_CA_FILE` com caminho absoluto para um arquivo de CA já existente naquele host. É um caminho, não o conteúdo do certificado. Caminhos relativos e caracteres de controle são rejeitados; não há fallback insecure. Nenhuma CA foi instalada no laboratório por esta alteração.

Os POSTs legados ainda usam `curl -k`: a segurança TLS de toda a pipeline não foi resolvida neste escopo. Revisar esse legado antes de uso fora de um ambiente controlado. O novo monitor não transmite credencial para um servidor cujo certificado não possa validar.

P2 tem limite de job de 65 minutos para comportar os limites individuais de espera, o sleep legado e margem de setup/finalização. Os testes verificam esse orçamento; isso não é uma medição de duração do laboratório.

Relatórios de erro mantêm o experimento, produtor, tentativa e upstream do contexto atual, sem reaproveitar documento de outro run. Se o POST de dispatch retornar um ID mas a confirmação falhar, o estado permanece unknown e preserva esse ID e a ref para reconciliação. O caminho disabled registra a opção sem disponibilizar token ao step.

Entradas e alvos com scope IPv6 são rejeitados, não reparados. Metadados do resumo são lidos por expansão de variáveis de ambiente Bash; campos env dos steps continuam usando o mapeamento Actions adequado.

## Contrato de eventos

Ambos os workflows aceitam workflow_dispatch e repository_dispatch do tipo `ondemand`. Para repository_dispatch, use campos estruturados em client_payload; strings de comandos em `args` não são interpretadas como shell.

Em P1, preserve seu argumento HCL atual em `terraform_variables` ou forneça o objeto `config` do provisionamento em client_payload. Não envie os dois. O HCL é passado como um único argumento para Terraform, sem shell; o objeto é escrito como var-file JSON privado. Não há provisionamento com inputs vazios por default.

Campos de controle para acrescentar ao seu payload existente:

```json
{
  "experiment_id": "ensaio-001",
  "cluster_name": "ensaio-001",
  "target_cluster_ip": "192.0.2.10",
  "dispatch_free5gc": false,
  "free5gc_ref": "ci/readiness-gated-dispatch",
  "minimum_ready_nodes": 1,
  "readiness_timeout_s": 900,
  "cluster_user": "packer"
}
```

O endereço acima é de documentação, não do laboratório. Este fragmento não substitui seu config Terraform nem o config_base64 do Free5GC. Quando o dispatch automático for habilitado, cluster_name e config_base64 são obrigatórios. Se houver mais de um endpoint nos outputs Terraform, target_cluster_ip precisa selecionar exatamente um deles; nenhum primeiro IP é escolhido silenciosamente.

O remetente automático chama o workflow deploy.yaml com ref explícita e transporta experiment_id, upstream_run_id e upstream_run_attempt. P2 mantém a referência à tentativa de P1 nos seus relatórios.

## Evidência e resultado do dispatch

A evidência usada no handoff precisa pertencer ao mesmo experimento, run e tentativa, ter três observações válidas e ser recente. O remetente valida o alvo antes do POST. A API é versionada em `2026-03-10`; a resposta com ID de execução é seguida de leitura desse run para confirmar branch, evento e identidade do experimento.

Não há retentativa cega de POST. Se a resposta for perdida ou a leitura posterior não confirmar a criação, o resultado fica desconhecido e deve ser reconciliado antes de reenviar. Isso não implementa exactly-once nem idempotência de todo o ciclo OSM/Terraform.

| Estado | Interpretação |
|---|---|
| readiness checking sem conclusão | Execução incompleta; não tratar como pronta |
| readiness ready | Cluster passou pelo gate observado |
| readiness timeout/failed | Handoff não liberado |
| dispatch disabled | Não houve tentativa de disparo pelo novo remetente |
| dispatch accepted | Run de P2 criado e lido de volta; não significa Free5GC funcional |
| dispatch blocked/rejected | Não houve confirmação de um dispatch autorizado |
| dispatch unknown | Reconciliar o run remoto; não reenviar automaticamente |

Artifacts `readiness-<run_id>-<run_attempt>` incluem JSON de prontidão e handoff. Não incluem contexto privado, config_base64, kubeconfig ou token. Os relatórios identificam o IP alvo e contagens de nós; sanitize identificadores antes de publicação científica. A retenção depende da política do repositório; exporte os dados necessários antes de expirar.

## Monitor de estado OSM

O monitor usa GET JSON em `/osm/nslcm/v1/ns_instances_content/{id}`, o endpoint de leitura utilizado pelo cliente oficial v16. Não tenta extrair palavras da saída YAML de `osm ns-show --literal`. A leitura valida o ID exato, campos do nível superior e ausência de chaves JSON duplicadas.

Somente `nsState=READY`, `currentOperation=IDLE`, `operational-status=running` e `config-status=configured` juntos são aceitos. Falha de configuração, BROKEN, DEGRADED e terminação não são sucesso, mesmo que outro campo ainda indique running. Estados de progresso reconhecidos mantêm a espera; formatos não suportados falham de forma fechada. A compatibilidade com a versão realmente instalada precisa de validação com registro sanitizado do laboratório.

A consulta é somente leitura, limitada por prazo e transporta o bearer por stdin do SSH/curl, não por argumento de processo. O relatório `metrics/ns-readiness.json` preserva a correlação com P1 e não publica a resposta NS completa. Timeout retorna 124; outros erros retornam código não zero. A criação de um ID NS é distinguida da conclusão OSM, que continua não sendo prova de tráfego UE→DN.

Defina minimum_ready_nodes conforme a topologia esperada: o default 1 não exige que futuros nós ainda não registrados já tenham ingressado. O gate não mede capacidade para o workload nem prova funcionamento do CoreDNS. Os POSTs e os nomes temporários remotos legados do deploy continuam precisando de uma revisão própria de concorrência/idempotência antes de ensaios paralelos.

Contrato de referência, consultado no código oficial v16.0.0:
- https://osm.etsi.org/gitweb/?p=osm/osmclient.git;a=blob_plain;f=osmclient/sol005/ns.py;hb=refs/tags/v16.0.0
- https://osm.etsi.org/gitweb/?p=osm/LCM.git;a=blob_plain;f=osm_lcm/ns.py;hb=refs/tags/v16.0.0

## Verificação local, apenas WSL/Docker

Na raiz do checkout:

```bash
python3 -m unittest discover -s tests -v
```

A workflow guard-tests executa somente os testes offline, sem Terraform apply/destroy, SSH real ou dispatch real. As consultas do laboratório e as operações OSM/Terraform só são executadas quando o workflow operacional for explicitamente disparado.

O backend de state, a recuperação após falhas de apply e a idempotência dos POSTs OSM continuam exigindo revisão própria. Não alteramos automaticamente o state existente ou a política de recuperação do laboratório. O relatório de pods é inspeção, não uma prova de conectividade 5G.

## Referências de implementação

- Kubernetes API health endpoints: https://kubernetes.io/docs/reference/using-api/health-checks/
- GitHub workflow dispatch API: https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event
- GitHub repository_dispatch: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#repository_dispatch
