-- Lumen 精选商城 MySQL 结构（由 shop/data/shop.db 的 30 张 SQLite 表逐列翻写）
-- 约定：utf8mb4/InnoDB；金额一律 *_cents 分为单位；布尔用 TINYINT(1)；JSON 字段用 JSON；时间用 DATETIME(3)（存 UTC）
SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

CREATE TABLE IF NOT EXISTS users (
  id VARCHAR(40) NOT NULL PRIMARY KEY,
  email VARCHAR(190) NOT NULL,
  phone VARCHAR(32),
  name VARCHAR(80) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  role VARCHAR(16) NOT NULL DEFAULT 'customer',
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  marketing_opt_in TINYINT(1) NOT NULL DEFAULT 0,
  balance_cents BIGINT NOT NULL DEFAULT 0,
  email_verified_at DATETIME(3),
  last_login_at DATETIME(3),
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  UNIQUE KEY uq_users_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS sessions (
  id VARCHAR(40) NOT NULL PRIMARY KEY,
  user_id VARCHAR(40) NOT NULL,
  token_hash VARCHAR(128) NOT NULL,
  guest_token VARCHAR(40),
  user_agent VARCHAR(255),
  ip VARCHAR(64),
  expires_at DATETIME(3) NOT NULL,
  revoked_at DATETIME(3),
  created_at DATETIME(3) NOT NULL,
  UNIQUE KEY uq_sessions_token (token_hash),
  KEY idx_sessions_user (user_id),
  CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS verification_tokens (
  id VARCHAR(40) NOT NULL PRIMARY KEY,
  user_id VARCHAR(40) NOT NULL,
  purpose VARCHAR(24) NOT NULL,
  token_hash VARCHAR(128) NOT NULL,
  expires_at DATETIME(3) NOT NULL,
  used_at DATETIME(3),
  created_at DATETIME(3) NOT NULL,
  UNIQUE KEY uq_vtoken_hash (token_hash),
  CONSTRAINT fk_vtoken_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS addresses (
  id VARCHAR(40) NOT NULL PRIMARY KEY,
  user_id VARCHAR(40) NOT NULL,
  receiver VARCHAR(60) NOT NULL,
  phone VARCHAR(32) NOT NULL,
  province VARCHAR(40) NOT NULL,
  city VARCHAR(40) NOT NULL,
  district VARCHAR(60) NOT NULL,
  detail VARCHAR(255) NOT NULL,
  postal VARCHAR(16),
  tag VARCHAR(24),
  is_default_shipping TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME(3) NOT NULL,
  KEY idx_addresses_user (user_id),
  CONSTRAINT fk_addresses_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS balance_entries (
  id VARCHAR(40) NOT NULL PRIMARY KEY,
  user_id VARCHAR(40) NOT NULL,
  amount_cents BIGINT NOT NULL,
  balance_after_cents BIGINT NOT NULL,
  kind VARCHAR(24) NOT NULL,
  reference VARCHAR(64),
  note VARCHAR(255),
  created_at DATETIME(3) NOT NULL,
  KEY idx_balance_user (user_id, created_at),
  CONSTRAINT fk_balance_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS categories (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  slug VARCHAR(80) NOT NULL,
  name VARCHAR(80) NOT NULL,
  parent_id BIGINT,
  description VARCHAR(255),
  icon VARCHAR(64),
  sort INT NOT NULL DEFAULT 0,
  active TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME(3) NOT NULL,
  UNIQUE KEY uq_categories_slug (slug),
  CONSTRAINT fk_categories_parent FOREIGN KEY (parent_id) REFERENCES categories(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS products (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  slug VARCHAR(120) NOT NULL,
  title VARCHAR(200) NOT NULL,
  subtitle VARCHAR(200),
  brand VARCHAR(80),
  description TEXT,
  category_id BIGINT,
  price_cents BIGINT NOT NULL DEFAULT 0,
  compare_at_cents BIGINT,
  currency CHAR(3) NOT NULL DEFAULT 'CNY',
  status VARCHAR(16) NOT NULL DEFAULT 'draft',
  tags JSON NOT NULL,
  attributes JSON NOT NULL,
  rating_avg DECIMAL(3,2) NOT NULL DEFAULT 0,
  rating_count INT NOT NULL DEFAULT 0,
  sold_count INT NOT NULL DEFAULT 0,
  featured TINYINT(1) NOT NULL DEFAULT 0,
  published_at DATETIME(3),
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  UNIQUE KEY uq_products_slug (slug),
  KEY idx_products_brand (brand),
  KEY idx_products_category (category_id),
  KEY idx_products_status (status),
  CONSTRAINT fk_products_category FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS product_images (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  product_id BIGINT NOT NULL,
  url VARCHAR(255) NOT NULL,
  alt VARCHAR(200),
  sort INT NOT NULL DEFAULT 0,
  KEY idx_images_product (product_id),
  CONSTRAINT fk_images_product FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS variants (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  product_id BIGINT NOT NULL,
  sku VARCHAR(80) NOT NULL,
  title VARCHAR(120) NOT NULL,
  options JSON NOT NULL,
  price_cents BIGINT NOT NULL,
  compare_at_cents BIGINT,
  stock_qty INT NOT NULL DEFAULT 0,
  reserved_qty INT NOT NULL DEFAULT 0,
  weight_gram INT NOT NULL DEFAULT 0,
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  image_url VARCHAR(255),
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  UNIQUE KEY uq_variants_sku (sku),
  KEY idx_variants_product (product_id),
  CONSTRAINT fk_variants_product FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS inventory_movements (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  variant_id BIGINT NOT NULL,
  delta INT NOT NULL,
  reason VARCHAR(32) NOT NULL,
  reference_type VARCHAR(32),
  reference_id VARCHAR(40),
  actor_user_id VARCHAR(40),
  note VARCHAR(255),
  created_at DATETIME(3) NOT NULL,
  KEY idx_movements_variant (variant_id),
  CONSTRAINT fk_movements_variant FOREIGN KEY (variant_id) REFERENCES variants(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS wishlist (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id VARCHAR(40) NOT NULL,
  product_id BIGINT NOT NULL,
  created_at DATETIME(3) NOT NULL,
  UNIQUE KEY uq_wishlist (user_id, product_id),
  CONSTRAINT fk_wishlist_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_wishlist_product FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS reviews (
  id VARCHAR(40) NOT NULL PRIMARY KEY,
  product_id BIGINT NOT NULL,
  user_id VARCHAR(40) NOT NULL,
  order_id VARCHAR(40),
  variant_label VARCHAR(120),
  rating TINYINT NOT NULL,
  title VARCHAR(200),
  body TEXT NOT NULL,
  images JSON NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'published',
  helpful_count INT NOT NULL DEFAULT 0,
  reply TEXT,
  replied_at DATETIME(3),
  created_at DATETIME(3) NOT NULL,
  UNIQUE KEY uq_review_once (user_id, product_id, order_id),
  KEY idx_reviews_product (product_id),
  CONSTRAINT fk_reviews_product FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
  CONSTRAINT fk_reviews_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS review_votes (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  review_id VARCHAR(40) NOT NULL,
  user_id VARCHAR(40) NOT NULL,
  created_at DATETIME(3) NOT NULL,
  UNIQUE KEY uq_review_vote (review_id, user_id),
  CONSTRAINT fk_vote_review FOREIGN KEY (review_id) REFERENCES reviews(id) ON DELETE CASCADE,
  CONSTRAINT fk_vote_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS coupons (
  id VARCHAR(40) NOT NULL PRIMARY KEY,
  code VARCHAR(40) NOT NULL,
  kind VARCHAR(16) NOT NULL,
  value BIGINT NOT NULL,
  min_subtotal_cents BIGINT NOT NULL DEFAULT 0,
  max_discount_cents BIGINT,
  usage_limit INT,
  used_count INT NOT NULL DEFAULT 0,
  per_user_limit INT NOT NULL DEFAULT 1,
  applies_to VARCHAR(80),
  starts_at DATETIME(3) NOT NULL,
  ends_at DATETIME(3),
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  free_shipping TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME(3) NOT NULL,
  UNIQUE KEY uq_coupons_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS shipping_plans (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  code VARCHAR(24) NOT NULL,
  name VARCHAR(60) NOT NULL,
  description VARCHAR(255),
  eta VARCHAR(60),
  flat_cents BIGINT NOT NULL DEFAULT 0,
  free_over_cents BIGINT,
  weight_limit_gram INT,
  active TINYINT(1) NOT NULL DEFAULT 1,
  sort INT NOT NULL DEFAULT 0,
  UNIQUE KEY uq_plans_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS carts (
  id VARCHAR(40) NOT NULL PRIMARY KEY,
  user_id VARCHAR(40),
  guest_token VARCHAR(40),
  currency CHAR(3) NOT NULL DEFAULT 'CNY',
  coupon_code VARCHAR(40),
  shipping_plan_code VARCHAR(24),
  merged_into VARCHAR(40),
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  -- MySQL 唯一索引允许多行 NULL，语义等同于旧库的 partial unique
  UNIQUE KEY uq_carts_user (user_id),
  UNIQUE KEY uq_carts_guest (guest_token),
  CONSTRAINT fk_carts_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS cart_items (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  cart_id VARCHAR(40) NOT NULL,
  product_id BIGINT NOT NULL,
  variant_id BIGINT NOT NULL,
  qty INT NOT NULL,
  unit_price_cents BIGINT NOT NULL,
  added_at DATETIME(3) NOT NULL,
  UNIQUE KEY uq_cart_variant (cart_id, variant_id),
  CONSTRAINT fk_cart_items_cart FOREIGN KEY (cart_id) REFERENCES carts(id) ON DELETE CASCADE,
  CONSTRAINT fk_cart_items_product FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
  CONSTRAINT fk_cart_items_variant FOREIGN KEY (variant_id) REFERENCES variants(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS orders (
  id VARCHAR(40) NOT NULL PRIMARY KEY,
  order_no VARCHAR(32) NOT NULL,
  user_id VARCHAR(40),
  status VARCHAR(24) NOT NULL DEFAULT 'pending_payment',
  payment_status VARCHAR(24) NOT NULL DEFAULT 'unpaid',
  fulfillment_status VARCHAR(24) NOT NULL DEFAULT 'unfulfilled',
  email VARCHAR(190) NOT NULL,
  phone VARCHAR(32),
  shipping_address JSON NOT NULL,
  billing_address JSON,
  shipping_plan_code VARCHAR(24),
  tracking_no VARCHAR(64),
  carrier VARCHAR(60),
  subtotal_cents BIGINT NOT NULL DEFAULT 0,
  discount_cents BIGINT NOT NULL DEFAULT 0,
  shipping_cents BIGINT NOT NULL DEFAULT 0,
  tax_cents BIGINT NOT NULL DEFAULT 0,
  total_cents BIGINT NOT NULL DEFAULT 0,
  paid_cents BIGINT NOT NULL DEFAULT 0,
  refunded_cents BIGINT NOT NULL DEFAULT 0,
  coupon_code VARCHAR(40),
  buyer_note VARCHAR(500),
  internal_note VARCHAR(500),
  expires_at DATETIME(3),
  placed_at DATETIME(3) NOT NULL,
  paid_at DATETIME(3),
  shipped_at DATETIME(3),
  delivered_at DATETIME(3),
  cancelled_at DATETIME(3),
  cancel_reason VARCHAR(255),
  updated_at DATETIME(3) NOT NULL,
  UNIQUE KEY uq_orders_no (order_no),
  KEY idx_orders_status (status),
  KEY idx_orders_user (user_id),
  CONSTRAINT fk_orders_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS order_items (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  order_id VARCHAR(40) NOT NULL,
  product_id BIGINT,
  variant_id BIGINT,
  title VARCHAR(200) NOT NULL,
  variant_title VARCHAR(120),
  sku VARCHAR(80),
  image_url VARCHAR(255),
  unit_price_cents BIGINT NOT NULL,
  qty INT NOT NULL,
  refunded_qty INT NOT NULL DEFAULT 0,
  KEY idx_order_items_order (order_id),
  CONSTRAINT fk_items_order FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
  CONSTRAINT fk_items_product FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL,
  CONSTRAINT fk_items_variant FOREIGN KEY (variant_id) REFERENCES variants(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS order_events (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  order_id VARCHAR(40) NOT NULL,
  kind VARCHAR(32) NOT NULL,
  message VARCHAR(500) NOT NULL,
  actor VARCHAR(40),
  created_at DATETIME(3) NOT NULL,
  KEY idx_order_events (order_id),
  CONSTRAINT fk_events_order FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS payments (
  id VARCHAR(40) NOT NULL PRIMARY KEY,
  order_id VARCHAR(40) NOT NULL,
  provider VARCHAR(24) NOT NULL,
  kind VARCHAR(16) NOT NULL DEFAULT 'sale',
  amount_cents BIGINT NOT NULL,
  currency CHAR(3) NOT NULL DEFAULT 'CNY',
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  intent_token VARCHAR(48),
  idempotency_key VARCHAR(64),
  gateway_ref VARCHAR(80),
  failure_reason VARCHAR(255),
  raw JSON,
  created_at DATETIME(3) NOT NULL,
  confirmed_at DATETIME(3),
  UNIQUE KEY uq_payment_intent (intent_token),
  UNIQUE KEY uq_payment_idem (idempotency_key),
  KEY idx_payments_order (order_id),
  CONSTRAINT fk_payments_order FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS coupon_redemptions (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  coupon_id VARCHAR(40) NOT NULL,
  order_id VARCHAR(40) NOT NULL,
  user_id VARCHAR(40),
  discount_cents BIGINT NOT NULL,
  created_at DATETIME(3) NOT NULL,
  UNIQUE KEY uq_redemption (coupon_id, order_id),
  CONSTRAINT fk_redemption_coupon FOREIGN KEY (coupon_id) REFERENCES coupons(id) ON DELETE CASCADE,
  CONSTRAINT fk_redemption_order FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS refund_requests (
  id VARCHAR(40) NOT NULL PRIMARY KEY,
  order_id VARCHAR(40) NOT NULL,
  user_id VARCHAR(40),
  kind VARCHAR(16) NOT NULL DEFAULT 'refund',
  item_ids JSON,
  amount_cents BIGINT NOT NULL,
  reason VARCHAR(255) NOT NULL,
  detail TEXT,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  admin_note VARCHAR(500),
  handled_by VARCHAR(40),
  handled_at DATETIME(3),
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  KEY idx_refund_order (order_id),
  KEY idx_refund_status (status, created_at),
  CONSTRAINT fk_refund_order FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
  CONSTRAINT fk_refund_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS reservations (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  order_id VARCHAR(40) NOT NULL,
  variant_id BIGINT NOT NULL,
  qty INT NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  created_at DATETIME(3) NOT NULL,
  resolved_at DATETIME(3),
  KEY idx_reservations_order (order_id),
  CONSTRAINT fk_reservations_order FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
  CONSTRAINT fk_reservations_variant FOREIGN KEY (variant_id) REFERENCES variants(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS settings (
  `key` VARCHAR(64) NOT NULL PRIMARY KEY,
  `value` TEXT NOT NULL,
  updated_at DATETIME(3) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS support_faqs (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  question VARCHAR(200) NOT NULL,
  keywords VARCHAR(255) NOT NULL,
  answer TEXT NOT NULL,
  kind VARCHAR(16) NOT NULL DEFAULT 'faq',
  link VARCHAR(255),
  active TINYINT(1) NOT NULL DEFAULT 1,
  sort INT NOT NULL DEFAULT 100,
  hits INT NOT NULL DEFAULT 0,
  updated_by VARCHAR(40),
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  KEY idx_faqs_active (active, sort)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS support_messages (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  conversation VARCHAR(40) NOT NULL,
  user_id VARCHAR(40),
  role VARCHAR(12) NOT NULL,
  content TEXT NOT NULL,
  intent VARCHAR(32),
  hits INT NOT NULL DEFAULT 0,
  meta JSON,
  created_at DATETIME(3) NOT NULL,
  KEY idx_support_conv (conversation, id),
  KEY idx_support_intent (intent, id),
  CONSTRAINT fk_support_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS analytics_events (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  kind VARCHAR(32) NOT NULL,
  user_id VARCHAR(40),
  session_id VARCHAR(40),
  product_id BIGINT,
  meta JSON,
  created_at DATETIME(3) NOT NULL,
  KEY idx_analytics_kind (kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS audit_logs (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id VARCHAR(40),
  actor_type VARCHAR(16) NOT NULL DEFAULT 'user',
  action VARCHAR(64) NOT NULL,
  entity VARCHAR(48),
  entity_id VARCHAR(48),
  meta JSON,
  ip VARCHAR(64),
  created_at DATETIME(3) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS outbox_messages (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  kind VARCHAR(32) NOT NULL,
  recipient VARCHAR(190) NOT NULL,
  subject VARCHAR(255) NOT NULL,
  body TEXT NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'queued',
  created_at DATETIME(3) NOT NULL,
  sent_at DATETIME(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 小马（LangGraph 智能体）──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_threads (
  id VARCHAR(40) NOT NULL PRIMARY KEY,
  user_id VARCHAR(40),
  guest_token VARCHAR(40),
  channel VARCHAR(16) NOT NULL DEFAULT 'web',
  status VARCHAR(16) NOT NULL DEFAULT 'open',
  summary TEXT,
  last_intent VARCHAR(32),
  turns INT NOT NULL DEFAULT 0,
  created_at DATETIME(3) NOT NULL,
  updated_at DATETIME(3) NOT NULL,
  KEY idx_threads_user (user_id, updated_at),
  KEY idx_threads_status (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_messages (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  thread_id VARCHAR(40) NOT NULL,
  role VARCHAR(12) NOT NULL,
  content TEXT NOT NULL,
  tool_calls JSON,
  intent VARCHAR(32),
  model VARCHAR(40),
  latency_ms INT,
  created_at DATETIME(3) NOT NULL,
  KEY idx_agent_messages_thread (thread_id, id),
  CONSTRAINT fk_agent_messages_thread FOREIGN KEY (thread_id) REFERENCES agent_threads(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_tool_calls (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  thread_id VARCHAR(40) NOT NULL,
  tool VARCHAR(48) NOT NULL,
  args JSON,
  result_count INT NOT NULL DEFAULT 0,
  status VARCHAR(16) NOT NULL DEFAULT 'ok',
  duration_ms INT,
  created_at DATETIME(3) NOT NULL,
  KEY idx_tool_calls_thread (thread_id),
  KEY idx_tool_calls_tool (tool, created_at),
  CONSTRAINT fk_tool_calls_thread FOREIGN KEY (thread_id) REFERENCES agent_threads(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 商品实物图的描述子缓存：按图找相似时只比这份数据，不用每次重新解图
CREATE TABLE IF NOT EXISTS product_image_signatures (
  url VARCHAR(255) NOT NULL PRIMARY KEY,
  sig JSON NOT NULL,
  pixels INT NOT NULL DEFAULT 0,
  products INT NOT NULL DEFAULT 0,
  built_at DATETIME(3) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 用户在聊天里上传的图：文件留在 data/uploads，描述子存这里供工具按编号取
CREATE TABLE IF NOT EXISTS agent_images (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  token VARCHAR(40) NOT NULL,
  thread_id VARCHAR(40),
  filename VARCHAR(200),
  mime VARCHAR(40) NOT NULL,
  url VARCHAR(255) NOT NULL,
  bytes INT NOT NULL DEFAULT 0,
  width INT NOT NULL DEFAULT 0,
  height INT NOT NULL DEFAULT 0,
  sig JSON NOT NULL,
  created_at DATETIME(3) NOT NULL,
  UNIQUE KEY uq_agent_images_token (token),
  KEY idx_agent_images_thread (thread_id),
  CONSTRAINT fk_agent_images_thread FOREIGN KEY (thread_id) REFERENCES agent_threads(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

SET FOREIGN_KEY_CHECKS = 1;
