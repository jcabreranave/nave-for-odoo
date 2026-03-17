# Nave Payment Provider for Odoo

Módulo de pago por redirección para Odoo eCommerce usando [Nave](https://navenegocios.com).

## Compatibilidad

| Odoo | Community | Enterprise |
|------|-----------|------------|
| 16.0 | ✅ | ✅ |
| 17.0 | ✅ | ✅ |
| 18.0 | ✅ | ✅ |

## Instalación

### Opción A — Desde el Odoo App Store
Buscar "Nave Payment" en https://apps.odoo.com e instalar desde ahí.

### Opción B — Manual
1. Copiar la carpeta `payment_nave/` al directorio `addons` de tu instalación de Odoo.
2. Reiniciar el servidor Odoo.
3. Ir a **Aplicaciones → Actualizar lista de aplicaciones**.
4. Buscar "Nave" e instalar.

## Configuración

1. **Contabilidad → Configuración → Proveedores de pago → Nave**
2. Completar:
   - **Client ID** — obtenido desde el portal B2B de Nave
   - **Client Secret** — obtenido desde el portal B2B de Nave
   - **POS ID** — identificador del punto de venta asignado por Nave
   - **Duración del link** — segundos que el link de pago estará vigente (default: 900)
3. Cambiar el estado a **Habilitado** (producción) o **Test** (sandbox).
4. Asegurarse de que **Publicado** esté activo para que aparezca en el checkout.

## Flujo de pago

```
Cliente → Checkout Odoo → [Nave crea intención] → Redirect a Nave
→ Cliente paga → Nave notifica via Webhook → Odoo actualiza transacción
→ Cliente vuelve → /nave/return → Odoo confirma estado → /payment/status
```

## Seguridad

- **Callback token**: token criptográfico de 256 bits de un solo uso incluido en la return URL.
- **Webhook secret**: token de 256 bits por transacción para validar notificaciones server-to-server.
- Comparación de tokens con `hmac.compare_digest()` (timing-safe, equivalente a `hash_equals()` de PHP).

## Desarrollo

```bash
# Estructura del módulo
payment_nave/
├── __manifest__.py
├── models/
│   ├── payment_provider.py      # Credenciales, token management, API client
│   └── payment_transaction.py   # Máquina de estados, cron
├── controllers/
│   └── main.py                  # /nave/return  /nave/webhook
├── data/
│   ├── payment_provider_data.xml
│   └── ir_cron_data.xml
├── views/
│   └── payment_nave_templates.xml
└── security/
    └── ir.model.access.csv
```

## Soporte

- Email: dev@navenegocios.com
- Web: https://navenegocios.com
