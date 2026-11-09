/* 全站交互层：所有数字与状态都来自本站接口，出错直接把后端文案显示出来。 */

(() => {
	'use strict';

	const $ = (sel, root) => (root || document).querySelector(sel);
	const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
	const money = (cents) => `¥${(Number(cents || 0) / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
	const dt = (v) => String(v || '').replace('T', ' ').slice(0, 16);
	const MOVE_REASON = { initial: '初始入库', sale: '下单扣减', refund_restock: '退货回库', manual: '后台调整', admin_adjust: '后台调整' };
	const moveReason = (v) => MOVE_REASON[v] || v || '—';
	const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
	const setCartCount = (count) => {
		const badge = $('#cartCount');
		if (badge) badge.textContent = String(count);
	};
	const flash = (el, message, isError) => {
		if (!el) return;
		el.textContent = message;
		el.style.color = isError ? '#b4543f' : '';
	};

	async function request(method, url, body) {
		const response = await fetch(url, {
			method,
			headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
			body: body === undefined ? undefined : JSON.stringify(body),
			credentials: 'same-origin',
		});
		let payload = null;
		try {
			payload = await response.json();
		} catch (_) {
			throw new Error(`站点没有返回内容（HTTP ${response.status}）`);
		}
		if (!payload || payload.ok !== true) {
			throw new Error((payload && payload.error && payload.error.message) || `请求失败（HTTP ${response.status}）`);
		}
		return payload.data;
	}

	const api = {
		get: (url) => request('GET', url),
		post: (url, body) => request('POST', url, body === undefined ? {} : body),
		patch: (url, body) => request('PATCH', url, body === undefined ? {} : body),
		put: (url, body) => request('PUT', url, body === undefined ? {} : body),
		del: (url) => request('DELETE', url),
	};

	// 传图走 multipart，不能带 JSON 头，错误文案仍然用后端返回的那句
	async function uploadForm(url, form) {
		const response = await fetch(url, { method: 'POST', body: form, credentials: 'same-origin' });
		let payload = null;
		try {
			payload = await response.json();
		} catch (_) {
			throw new Error(`站点没有返回内容（HTTP ${response.status}）`);
		}
		if (!payload || payload.ok !== true) {
			throw new Error((payload && payload.error && payload.error.message) || `请求失败（HTTP ${response.status}）`);
		}
		return payload.data;
	}

	function bindStepper(root, onChange) {
		if (!root) return;
		const input = $('input', root);
		if (!input) return;
		$$('button', root).forEach((button) => {
			button.addEventListener('click', () => {
				const step = Number(button.dataset.step || (button.dataset.act === 'dec' ? -1 : 1));
				const max = Number(input.max || 999);
				const next = Math.min(max, Math.max(Number(input.min || 1), Number(input.value || 1) + step));
				if (next === Number(input.value)) return;
				input.value = String(next);
				onChange(next);
			});
		});
		input.addEventListener('change', () => onChange(Math.max(Number(input.min || 1), Number(input.value || 1))));
	}

	/* ---------- 商品详情 ---------- */
	function initProductPage() {
		const form = $('#buyForm');
		if (!form) return;
		const variants = JSON.parse(form.dataset.variants || '[]');
		const hidden = $('#variantId');
		const qtyInput = $('#qty');
		const hint = $('#buyHint');
		const selected = {};

		const applyVariant = () => {
			const groups = $$('.opt-chip.on').map((chip) => chip.dataset.group);
			const match = variants.find((v) => groups.every((g) => String((v.options || {})[g]) === selected[g])) || variants[0];
			if (!match) return;
			hidden.value = String(match.id);
			$('#priceText').textContent = money(match.price_cents);
			$('#stockText').textContent = match.available ? `可售 ${match.available} 件` : '暂时售罄';
			qtyInput.max = String(Math.max(1, match.available));
			qtyInput.value = '1';
			if (match.image_url) $('#mainImage').src = match.image_url;
		};

		$$('.opt-chip').forEach((chip) => {
			chip.addEventListener('click', () => {
				$$(`.opt-chip[data-group="${chip.dataset.group}"]`).forEach((sibling) => sibling.classList.remove('on'));
				chip.classList.add('on');
				selected[chip.dataset.group] = chip.dataset.value;
				applyVariant();
			});
		});
		$$('.opt-chip.on').forEach((chip) => {
			selected[chip.dataset.group] = chip.dataset.value;
		});
		if ($$('.opt-chip').length && variants.length) applyVariant();

		const addToCart = async () => {
			const variantId = Number(hidden.value);
			if (!variantId) {
				flash(hint, '这件商品还没有上架规格，可以先问小马。', true);
				return false;
			}
			try {
				const view = await api.post('/api/cart/items', { variant_id: variantId, qty: Number(qtyInput.value || 1) });
				setCartCount(view.count);
				flash(hint, `已加入购物车，购物车共 ${view.count} 件。`);
				return true;
			} catch (err) {
				flash(hint, err.message, true);
				return false;
			}
		};

		form.addEventListener('submit', async (event) => {
			event.preventDefault();
			await addToCart();
		});
		const buyNow = $('#buyNow');
		if (buyNow) {
			buyNow.addEventListener('click', async () => {
				if (await addToCart()) window.location.href = '/checkout';
			});
		}
		$$('.thumb').forEach((thumb) => {
			thumb.addEventListener('click', () => {
				$$('.thumb').forEach((t) => t.classList.remove('on'));
				thumb.classList.add('on');
				$('#mainImage').src = thumb.dataset.src;
			});
		});
	}

	/* ---------- 购物车 ---------- */
	function initCartPage() {
		const list = $('.cart-list');
		if (!list) return;
		const note = $('.summary-note');
		let busy = false;

		const paintQuote = (quote) => {
			$('#sumSubtotal').textContent = money(quote.subtotal_cents);
			$('#sumCouponLabel').textContent = quote.coupon ? `优惠码（${quote.coupon.code}）` : '优惠码';
			$('#sumCoupon').textContent = quote.discount_cents ? `-${money(quote.discount_cents)}` : '未使用';
			$('#sumShippingLabel').textContent = `运费 · ${quote.plan.name}`;
			$('#sumShipping').textContent = quote.shipping_cents ? money(quote.shipping_cents) : '免费';
			$('#sumTax').textContent = money(quote.tax_cents);
			$('#sumTotal').textContent = money(quote.total_cents);
			setCartCount(quote.count);
		};

		const refresh = async () => {
			if (busy) return;
			busy = true;
			try {
				const quote = await api.post('/api/checkout/quote', {});
				quote.items.forEach((item) => {
					const row = $(`.cart-row[data-item="${item.id}"]`, list);
					if (!row) return;
					$('.cart-line', row).textContent = money(item.line_total_cents);
					const input = $('input[data-act="qty"]', row);
					if (input) {
						input.max = String(Math.max(1, item.available));
						input.value = String(item.qty);
					}
				});
				paintQuote(quote);
			} catch (err) {
				busy = false;
				flash(note, err.message, true);
				return;
			}
			busy = false;
		};

		$$('.cart-row', list).forEach((row) => {
			const id = Number(row.dataset.item);
			bindStepper($('.qty', row), async () => {
				try {
					await api.patch(`/api/cart/items/${id}`, { qty: Number($('input[data-act="qty"]', row).value) });
				} catch (err) {
					flash(note, err.message, true);
				}
				await refresh();
			});
			$('[data-act="remove"]', row).addEventListener('click', async () => {
				try {
					const view = await api.del(`/api/cart/items/${id}`);
					if (!view.items.length) {
						window.location.href = '/cart';
						return;
					}
					row.remove();
					setCartCount(view.count);
					await refresh();
				} catch (err) {
					flash(note, err.message, true);
					await refresh();
				}
			});
		});

		const clear = $('#clearCart');
		if (clear) {
			clear.addEventListener('click', async () => {
				if (!confirm('清空购物车？')) return;
				await api.del('/api/cart');
				window.location.href = '/cart';
			});
		}

		const plan = $('#planSelect');
		if (plan) {
			plan.addEventListener('change', async () => {
				await api.post('/api/cart/plan', { plan_code: plan.value });
				await refresh();
			});
		}

		const couponInput = $('#couponInput');
		const apply = $('#applyCoupon');
		if (apply && couponInput) {
			apply.addEventListener('click', async () => {
				try {
					await api.post('/api/cart/coupon', { code: couponInput.value.trim() });
					await refresh();
					flash(note, couponInput.value.trim() ? `优惠码 ${couponInput.value.trim()} 已生效。` : '已取消优惠码。');
				} catch (err) {
					flash(note, err.message, true);
				}
			});
		}
	}

	/* ---------- 结算 ---------- */
	function initCheckout() {
		const form = $('#checkoutForm');
		if (!form) return;
		const msg = $('#checkoutMsg');
		const button = $('#placeBtn');
		const planCode = () => ($('input[name="plan"]:checked') || {}).value || 'STANDARD';
		const providerCode = () => ($('input[name="provider"]:checked') || {}).value || 'alipay';
		const field = (id) => (document.getElementById(id) || {}).value || '';

		$$('input[name="plan"], input[name="provider"]').forEach((radio) => {
			radio.addEventListener('change', () => {
				$$('.opt-card', radio.closest('.opt-list')).forEach((card) => card.classList.toggle('on', $('input', card).checked));
			});
			if (radio.checked) radio.closest('.opt-card') && radio.closest('.opt-card').classList.add('on');
		});
		$$('.opt-card input[type="radio"]').forEach((radio) => {
			radio.addEventListener('click', (event) => event.stopPropagation());
		});
		$$('.opt-card').forEach((card) => {
			card.addEventListener('click', () => {
				const input = $('input', card);
				if (!input || input.name !== 'plan') return;
				input.checked = true;
				input.dispatchEvent(new Event('change', { bubbles: true }));
			});
		});

		$$('.addr-card').forEach((card) => {
			card.addEventListener('click', () => {
				$$('.addr-card').forEach((sibling) => sibling.classList.remove('on'));
				card.classList.add('on');
				const map = { receiver: 'receiver', phone: 'rphone', province: 'province', city: 'city', district: 'district', detail: 'detail', postal: 'postal' };
				Object.entries(map).forEach(([key, id]) => {
					const el = document.getElementById(id);
					if (el && card.dataset[key] !== undefined) el.value = card.dataset[key];
				});
			});
		});

		form.addEventListener('submit', async (event) => {
			event.preventDefault();
			if (!form.reportValidity()) return;
			button.disabled = true;
			flash(msg, '正在锁库存并创建订单…');
			try {
				const order = await api.post('/api/checkout/place', {
					contact: { email: field('email'), phone: field('phone') },
					address: {
						receiver: field('receiver'),
						phone: field('rphone'),
						province: field('province'),
						city: field('city'),
						district: field('district'),
						detail: field('detail'),
						postal: field('postal'),
					},
					plan_code: planCode(),
					note: field('buyerNote'),
				});
				flash(msg, `订单 ${order.order_no} 已创建，正在走模拟支付…`);
				try {
					await api.post('/api/checkout/pay', { order_no: order.order_no, provider: providerCode() });
					flash(msg, '支付成功，正在打开订单详情…');
				} catch (err) {
					flash(msg, `订单 ${order.order_no} 已创建，但支付没成功：${err.message}。订单页可以重试或换支付方式。`, true);
				}
				setTimeout(() => {
					window.location.href = `/order/${order.order_no}`;
				}, 1000);
			} catch (err) {
				flash(msg, err.message, true);
				button.disabled = false;
			}
		});

		$$('input[name="plan"]').forEach((radio) => {
			radio.addEventListener('change', async () => {
				try {
					const quote = await api.post('/api/checkout/quote', { plan_code: planCode() });
					$('#totalText').textContent = money(quote.total_cents);
				} catch (err) {
					flash(msg, err.message, true);
				}
			});
		});
	}

	/* ---------- 账户 ---------- */
	function initAccount() {
		$$('.tab').forEach((tab) => {
			tab.addEventListener('click', () => {
				$$('.tab').forEach((other) => other.classList.toggle('on', other === tab));
				const login = $('#loginForm');
				const register = $('#registerForm');
				if (login && register) {
					login.hidden = tab.dataset.tab !== 'login';
					register.hidden = tab.dataset.tab !== 'register';
				}
			});
		});

		const msg = $('#authMsg');
		const submitAuth = async (url, body) => {
			try {
				const me = await api.post(url, body);
				// 管理员登录后要跳到后台那个端口，地址由模板注入的 LUMEN_URLS 给出
				window.location.href = me && me.role === 'admin' ? (window.LUMEN_URLS.admin || '') + '/admin' : '/account';
			} catch (err) {
				flash(msg, err.message, true);
			}
		};
		const login = $('#loginForm');
		if (login) {
			login.addEventListener('submit', (event) => {
				event.preventDefault();
				submitAuth('/api/login', { email: $('#lEmail').value.trim(), password: $('#lPass').value });
			});
		}
		const register = $('#registerForm');
		if (register) {
			register.addEventListener('submit', (event) => {
				event.preventDefault();
				submitAuth('/api/register', { name: $('#rName').value.trim(), email: $('#rEmail').value.trim(), password: $('#rPass').value });
			});
		}
		const logout = $('#logoutBtn');
		if (logout) {
			logout.addEventListener('click', async () => {
				await api.post('/api/logout');
				window.location.href = '/';
			});
		}

		const saveAddr = $('#saveAddr');
		if (saveAddr) {
			const target = $('.addr-form .filter-hint');
			saveAddr.addEventListener('click', async () => {
				const value = (id) => ($( '#' + id ) || {}).value?.trim() || '';
				try {
					await api.post('/api/addresses', {
						receiver: value('ad_receiver'),
						phone: value('ad_phone'),
						tag: value('ad_tag'),
						province: value('ad_province'),
						city: value('ad_city'),
						district: value('ad_district'),
						detail: value('ad_detail'),
						is_default: $('#ad_default').checked,
					});
					window.location.reload();
				} catch (err) {
					flash(target, err.message, true);
				}
			});
		}
		$$('[data-act="del-addr"]').forEach((button) => {
			button.addEventListener('click', async () => {
				if (!confirm('删除这条收货地址？')) return;
				try {
					await api.del(`/api/addresses/${button.closest('.addr-item').dataset.id}`);
					window.location.reload();
				} catch (err) {
					alert(err.message);
				}
			});
		});

		const topup = $('#topupForm');
		if (topup) {
			topup.addEventListener('submit', async (event) => {
				event.preventDefault();
				try {
					const result = await api.post('/api/wallet/topup', { amount_cents: Math.round(Number($('#topupYuan').value || 0) * 100) });
					$('#walletBalance').textContent = money(result.balance_cents);
					flash($('#topupMsg'), '充值成功，刷新页面可以看到这条流水。');
				} catch (err) {
					flash($('#topupMsg'), err.message, true);
				}
			});
		}
	}

	/* ---------- 订单详情 ---------- */
	function initOrderPage() {
		const msg = $('#orderMsg');
		$$('[data-act="pay"], [data-act="cancel"]').forEach((button) => {
			button.addEventListener('click', async () => {
				const ref = button.dataset.order;
				const cancel = button.dataset.act === 'cancel';
				if (cancel && !confirm('取消这笔待付款订单？库存与优惠码会释放。')) return;
				button.disabled = true;
				try {
					if (cancel) {
						await api.post(`/api/orders/${ref}/cancel`, { reason: '用户取消' });
						flash(msg, '订单已取消。');
					} else {
						await api.post('/api/checkout/pay', { order_no: ref, provider: button.dataset.provider || 'alipay' });
						flash(msg, '支付成功，正在刷新…');
					}
					setTimeout(() => window.location.reload(), 700);
				} catch (err) {
					flash(msg, err.message, true);
					button.disabled = false;
				}
			});
		});

		const refund = $('#refundForm');
		if (refund) {
			refund.addEventListener('submit', async (event) => {
				event.preventDefault();
				const reason = $('#rfReason').value.trim();
				try {
					await api.post(`/api/orders/${refund.dataset.order}/refund-requests`, {
						kind: $('#rfKind').value,
						reason,
						amount_cents: Math.round(Number($('#rfAmount').value || 0) * 100),
					});
					flash(msg, '退款申请已提交，商家处理后会写进订单进度。');
					setTimeout(() => window.location.reload(), 900);
				} catch (err) {
					flash(msg, err.message, true);
				}
			});
		}
	}

	/* ---------- 小马聊天 ---------- */
	const threadKey = 'lumen_xiaoma_thread';

	// 回复里的 LM 订单号一律变成可点的订单详情链接（离线路由与 DeepSeek 两条路径都走这里）
	const ORDER_NO_RE = /LM\d{6}[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{6}/gi;

	// 工具标签的量词按工具本身区分，避免出现「订单查询 · 1 条」这种对不上的说法
	const TOOL_UNITS = {
		search_products: '件商品',
		recommend_products: '件商品',
		outfit_recommend: '件单品',
		find_similar_by_image: '件相似商品',
		product_detail: '件商品',
		query_order: '笔订单',
		check_inventory: '个规格',
		shipping_options: '个方案',
		coupons_available: '个优惠码',
		store_faq: '条问答',
	};

	function linkifyOrders(html) {
		return html.replace(ORDER_NO_RE, (no) => {
			const upper = no.toUpperCase();
			return `<a class="chat-order" href="/order/${upper}" title="打开订单详情">${upper}</a>`;
		});
	}

	function bubbleRow(role, html) {
		const row = document.createElement('div');
		row.className = `chat-row ${role === 'user' ? 'is-user' : 'is-agent'}`;
		const bubble = document.createElement('div');
		bubble.className = 'chat-bubble';
		bubble.innerHTML = html;
		row.appendChild(bubble);
		return row;
	}

	function answerHtml(data) {
		// 模型偶尔吐 markdown 的 **强调**，气泡不渲染 markdown：转成粗体，别把星号留在屏幕上
		let html = linkifyOrders(esc(data.reply || '').replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')).replace(/\n/g, '<br>');
		if (data.degraded) html += '<div class="chat-tools"><span>模型不可用，已走站内意图路由</span></div>';
		const tools = data.tool_calls || [];
		if (tools.length) {
			html += `<div class="chat-tools">${tools
				.map((t) => {
					const prev = (t.preview || [])
						.map((x) => (typeof x === 'string' ? x : x.title || x.name || x.order_no || x.code || ''))
						.filter(Boolean)
						.slice(0, 2);
					const unit = TOOL_UNITS[t.tool] || '条';
					const n = t.result_count != null ? t.result_count : t.count != null ? t.count : (t.preview || []).length;
					return `<span>${esc(t.label || t.tool)} · ${n} ${unit}${prev.length ? `：${esc(prev.join('、'))}` : ''}</span>`;
				})
				.join('')}</div>`;
		}
		const cards = data.cards || [];
		if (cards.length) {
			html += `<div class="chat-cards">${cards
				.map(
					(c) =>
						`<a href="${esc(c.url || `/product/${c.slug}`)}">` +
						(c.image ? `<img src="${esc(c.image)}" alt="">` : '<i class="chat-nophoto">暂无实物图</i>') +
						(c.similarity_pct ? `<i class="chat-similar">相似 ${esc(c.similarity_pct)}%</i>` : '') +
						// 穿搭位压在商品图下角：凑一套时卡片要能和回复里的「主件/内搭/鞋履」对上
						(c.role ? `<i class="chat-role">${esc(c.role)}</i>` : '') +
						`<span><b>${esc(c.title)}</b><em>${esc(c.price_cents != null ? money(c.price_cents) : '¥' + c.price_yuan)}${
							c.available ? ` · 可售 ${esc(c.available)}` : ' · 暂时售罄'
						}</em></span></a>`
				)
				.join('')}</div>`;
		}
		return html;
	}

	// 只发图不打字时补的那句话，要和后端 agent_api.IMAGE_ONLY_QUESTION 一字不差，
	// 否则用户气泡里看到的和后台复盘里记的不是同一句
	const PICTURE_ONLY_QUESTION = '这张图在站内有相似的吗';

	function initChat(widget) {
		const log = $(widget.log);
		const form = $(widget.form);
		if (!log || !form) return null;
		const input = $(widget.input);
		const quick = $(widget.quick);
		const file = $(widget.file);
		const pendingBox = $(widget.pending);
		let pending = false;
		let staged = null;
		let maxBytes = 5 * 1024 * 1024;

		const clearStaged = () => {
			staged = null;
			if (file) file.value = '';
			if (pendingBox) {
				pendingBox.hidden = true;
				pendingBox.innerHTML = '';
			}
		};

		const renderStaged = () => {
			if (!pendingBox) return;
			if (!staged) {
				clearStaged();
				return;
			}
			pendingBox.hidden = false;
			pendingBox.innerHTML =
				`<span class="chat-staged"><img src="${esc(staged.url)}" alt="待上传的图">` +
				`<span><b>${esc(staged.name)}</b><em>发送后按主色、明暗和轮廓找相似</em></span>` +
				`<button type="button" class="chat-staged-off" aria-label="移除这张图">移除</button></span>`;
			const off = $('.chat-staged-off', pendingBox);
			if (off) off.addEventListener('click', clearStaged);
		};

		const warn = (text) => {
			const row = bubbleRow('agent', esc(text));
			$('.chat-bubble', row).classList.add('is-error');
			log.appendChild(row);
			log.scrollTop = log.scrollHeight;
		};

		const stage = (picked, fallbackName) => {
			if (!picked) return;
			if (!/^image\/(png|jpe?g|webp)$/.test(picked.type || '')) {
				warn('只支持 JPG、PNG、WebP 图片，这张传不了。');
				return;
			}
			if (picked.size > maxBytes) {
				warn(`这张图 ${(picked.size / 1048576).toFixed(1)} MB，超过 ${Math.round(maxBytes / 1048576)} MB 上限，压缩后再传。`);
				return;
			}
			staged = { file: picked, name: picked.name || fallbackName || '图片', url: URL.createObjectURL(picked) };
			renderStaged();
		};

		if (file) file.addEventListener('change', () => stage(file.files && file.files[0]));
		// 直接把图拖进对话区也算传图，不用先点图标找按钮
		log.addEventListener('dragover', (event) => {
			if (event.dataTransfer && Array.from(event.dataTransfer.types || []).includes('Files')) event.preventDefault();
		});
		log.addEventListener('drop', (event) => {
			const picked = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
			if (!picked) return;
			event.preventDefault();
			stage(picked);
		});
		// 截图后 Ctrl+V 直接进待发送区；剪贴板里没有图时不拦，正常粘文字
		const pasteImage = (event) => {
			const items = (event.clipboardData && event.clipboardData.items) || null;
			if (!items) return;
			for (let i = 0; i < items.length; i += 1) {
				const item = items[i];
				if (!item || item.kind !== 'file' || !/^image\//.test(item.type || '')) continue;
				const picked = item.getAsFile();
				if (!picked) continue;
				event.preventDefault();
				stage(picked, '粘贴的截图.png');
				return;
			}
		};
		form.addEventListener('paste', pasteImage);
		// 面板开着的时候，焦点不在输入框里粘贴也算；关掉时不去抢别的页面的粘贴
		document.addEventListener('paste', (event) => {
			if (event.defaultPrevented || !form.getClientRects().length) return;
			pasteImage(event);
		});

		api.get('/api/agent/state')
			.then((state) => {
				if (state && state.upload && state.upload.max_bytes) maxBytes = Number(state.upload.max_bytes);
				if (state && state.upload && file) file.accept = (state.upload.accept || []).join(',');
			})
			.catch(() => undefined);

		const send = async (text) => {
			const message = String(text || '').trim();
			if ((!message && !staged) || pending) return;
			pending = true;
			const shot = staged;
			const bubble = shot
				? `<img class="chat-thumb" src="${esc(shot.url)}" alt="我上传的图">${esc(message || PICTURE_ONLY_QUESTION)}`
				: esc(message);
			log.appendChild(bubbleRow('user', bubble));
			input.value = '';
			clearStaged();
			log.scrollTop = log.scrollHeight;
			const typing = bubbleRow('is-agent', shot ? '正在读这张图，算主色与轮廓…' : '正在查站内数据…');
			log.appendChild(typing);
			log.scrollTop = log.scrollHeight;
			const fail = (problem) => {
				typing.remove();
				warn(problem.message || '出错了');
			};
			let imageToken = '';
			try {
				if (shot) {
					const body = new FormData();
					body.append('file', shot.file, shot.file.name);
					body.append('thread_id', localStorage.getItem(threadKey) || '');
					const info = await uploadForm('/api/agent/images', body);
					imageToken = info.token;
				}
				const data = await api.post('/api/agent/messages', {
					message,
					image_token: imageToken,
					thread_id: localStorage.getItem(threadKey) || null,
				});
				localStorage.setItem(threadKey, data.thread_id);
				typing.remove();
				log.appendChild(bubbleRow('agent', answerHtml(data)));
			} catch (problem) {
				fail(problem);
			} finally {
				pending = false;
				log.scrollTop = log.scrollHeight;
			}
		};

		form.addEventListener('submit', (event) => {
			event.preventDefault();
			send(input.value);
		});

		if (quick) {
			api.get('/api/agent/quick')
				.then((items) => {
					quick.innerHTML = items.map((q) => `<button type="button" data-q="${esc(q)}">${esc(q)}</button>`).join('');
					$$('button', quick).forEach((button) => button.addEventListener('click', () => send(button.dataset.q)));
				})
				.catch(() => undefined);
		}

		// 刷新后把这段对话补回来：thread_id 一直存在本地，小马记得问过什么，界面也得记得，
		// 否则上一轮推荐的商品卡一刷新就没了，用户只能重新问一遍
		const restore = () => {
			const tid = localStorage.getItem(threadKey);
			if (!tid) return;
			api.get(`/api/agent/history?thread_id=${encodeURIComponent(tid)}`)
				.then((data) => {
					const rows = (data && data.messages) || [];
					rows.forEach((m) => {
						if (m.role === 'user') log.appendChild(bubbleRow('user', linkifyOrders(esc(m.content))));
						else if (m.role === 'assistant') {
							log.appendChild(bubbleRow('agent', answerHtml({ reply: m.content, tool_calls: m.tool_calls, cards: m.cards })));
						}
					});
					if (rows.length) log.scrollTop = log.scrollHeight;
				})
				.catch(() => undefined);
		};
		restore();

		return { send, log };
	}

	/* 悬浮窗几何：位置与尺寸存本地，刷新后回到上次放的地方；放大只是临时的 */
	const BOX_KEY = 'lumen_xiaoma_box';
	const MIN_W = 300;
	const MIN_H = 320;

	function initXiaomaBox(panel) {
		const head = $('.xiaoma-head', panel);
		const grip = $('#xiaomaGrip', panel);
		const maxBtn = $('#xiaomaMax', panel);
		let placed = false;
		let before = null;

		const read = () => {
			try {
				return JSON.parse(localStorage.getItem(BOX_KEY) || 'null');
			} catch (err) {
				return null;
			}
		};
		const clamp = (box) => {
			box.w = Math.max(MIN_W, Math.min(box.w, window.innerWidth - 16));
			box.h = Math.max(MIN_H, Math.min(box.h, window.innerHeight - 16));
			box.x = Math.min(Math.max(box.x, 8), Math.max(8, window.innerWidth - box.w - 8));
			box.y = Math.min(Math.max(box.y, 8), Math.max(8, window.innerHeight - box.h - 8));
			return box;
		};
		const current = () => {
			const r = panel.getBoundingClientRect();
			return { x: r.left, y: r.top, w: r.width, h: r.height };
		};
		const apply = (box) => {
			panel.style.left = `${Math.round(box.x)}px`;
			panel.style.top = `${Math.round(box.y)}px`;
			panel.style.right = 'auto';
			panel.style.bottom = 'auto';
			panel.style.width = `${Math.round(box.w)}px`;
			panel.style.height = `${Math.round(box.h)}px`;
			placed = true;
		};
		const restore = () => {
			if (panel.hidden) return; // 收起时量不到尺寸，等打开那一刻再套用上次的几何
			const box = read();
			if (box && box.w && box.h) apply(clamp(box));
		};
		const setMax = (on) => {
			if (on) {
				before = clamp(current());
				panel.classList.add('is-max');
				apply({ x: 12, y: 12, w: window.innerWidth - 24, h: window.innerHeight - 24 });
				if (maxBtn) maxBtn.setAttribute('aria-label', '还原客服窗');
			} else {
				panel.classList.remove('is-max');
				if (before) apply(before);
				if (maxBtn) maxBtn.setAttribute('aria-label', '放大客服窗');
			}
		};

		if (maxBtn) {
			maxBtn.addEventListener('click', () => setMax(!panel.classList.contains('is-max')));
		}
		const grab = (event, mode) => {
			if (panel.classList.contains('is-max') || window.innerWidth < 560) return;
			event.preventDefault();
			const start = current();
			const px = event.clientX;
			const py = event.clientY;
			const move = (e) => {
				const dx = e.clientX - px;
				const dy = e.clientY - py;
				apply(
					mode === 'drag'
						? clamp({ x: start.x + dx, y: start.y + dy, w: start.w, h: start.h })
						: clamp({ x: start.x, y: start.y, w: start.w + dx, h: start.h + dy }),
				);
			};
			const up = () => {
				window.removeEventListener('pointermove', move);
				window.removeEventListener('pointerup', up);
				localStorage.setItem(BOX_KEY, JSON.stringify(clamp(current())));
			};
			window.addEventListener('pointermove', move);
			window.addEventListener('pointerup', up);
		};
		if (head) {
			head.addEventListener('pointerdown', (e) => {
				if (e.target.closest('button')) return;
				grab(e, 'drag');
			});
			head.addEventListener('dblclick', () => setMax(!panel.classList.contains('is-max')));
		}
		if (grip) grip.addEventListener('pointerdown', (e) => grab(e, 'resize'));
		window.addEventListener('resize', () => {
			if (panel.classList.contains('is-max')) apply({ x: 12, y: 12, w: window.innerWidth - 24, h: window.innerHeight - 24 });
			else if (placed) apply(clamp(current()));
		});

		return { restore };
	}

	function initXiaoma() {
		const fab = $('#xiaomaFab');
		const panel = $('#xiaomaPanel');
		if (!fab || !panel) return;
		const chat = initChat({ log: '#xiaomaLog', form: '#xiaomaForm', input: '#xiaomaInput', quick: '#xiaomaQuick', file: '#xiaomaFile', pending: '#xiaomaPending' });
		const box = initXiaomaBox(panel);
		const open = (show) => {
			panel.hidden = !show;
			fab.style.display = show ? 'none' : '';
			if (show) {
				box.restore();
				panel.querySelector('input').focus();
			}
		};
		fab.addEventListener('click', () => open(true));
		const close = $('#xiaomaClose');
		if (close) close.addEventListener('click', () => open(false));
		$$('.faq-ask').forEach((button) => {
			button.addEventListener('click', () => {
				open(true);
				if (chat) chat.send(button.dataset.q);
			});
		});
	}

	function initSupportPage() {
		const chat = initChat({ log: '#supportLog', form: '#supportForm', input: '#supportInput', quick: '#supportQuick', file: '#supportFile', pending: '#supportPending' });
		if (!chat) return;
		$$('.faq-ask').forEach((button) => button.addEventListener('click', () => chat.send(button.dataset.q)));
	}

	/* ---------- 后台 ---------- */
	function initAdmin() {
		if (!document.querySelector('.admin-shell')) return;
		const loaded = {};
		const tbody = (name) => $(`#${name}Rows`);
		const rows = (name, html) => {
			const box = tbody(name);
			if (box) box.innerHTML = html;
		};
		const none = (span) => `<tr><td colspan="${span}">没有符合条件的记录</td></tr>`;
		const act = (label, kind, id, extra) => `<button type="button" class="mini" data-kind="${kind}" data-id="${esc(id)}" ${extra || ''}>${esc(label)}</button>`;

		const panes = {
			products: async () => {
				const form = $('#productSearch');
				const params = new URLSearchParams({ q: form.q.value.trim(), status: form.status.value, per_page: '30' });
				const data = await api.get(`/api/admin/products?${params}`);
				rows(
					'product',
					data.items
						.map(
							(p) => `<tr data-id="${p.id}">
							<td><a href="/product/${esc(p.slug)}">${esc(p.title)}</a></td>
							<td>${esc(p.brand || '—')}</td>
							<td>${money(p.price_cents)}<span class="muted"> / 起</span></td>
							<td><select data-field="status"><option value="active" ${p.status === 'active' ? 'selected' : ''}>在售</option><option value="draft" ${p.status === 'draft' ? 'selected' : ''}>草稿</option><option value="archived" ${p.status === 'archived' ? 'selected' : ''}>下架</option></select></td>
							<td><input type="checkbox" data-field="featured" ${p.featured ? 'checked' : ''} style="width:auto"></td>
							<td class="row-act">${act('保存', 'product', p.id)}</td>
						</tr>`
						)
						.join('') || none(6)
				);
			},
			inventory: async () => {
				const q = ($('#variantSearch') || {}).q ? $('#variantSearch').q.value.trim() : '';
				const [items, movements] = await Promise.all([api.get(`/api/admin/variants?q=${encodeURIComponent(q)}`), api.get('/api/admin/inventory/movements?limit=15')]);
				rows(
					'variant',
					items
						.map(
							(v) => `<tr data-id="${v.variant_id}">
							<td>${esc(v.product_title)}<span class="muted"> · ${esc(v.title)}</span></td>
							<td>${esc(v.sku)}</td>
							<td><input type="number" step="0.01" min="0.01" data-field="price_yuan" value="${(v.price_cents / 100).toFixed(2)}"></td>
							<td><input type="number" data-field="stock_qty" value="${v.stock_qty}"></td>
							<td>${v.reserved_qty}</td>
							<td class="${v.available <= 0 ? 'danger' : v.available <= 5 ? 'warn' : ''}">${v.available}</td>
							<td>${esc(v.status)}</td>
							<td class="row-act">${act('保存', 'variant', v.variant_id)}</td>
						</tr>`
						)
						.join('') || none(8)
				);
				rows(
					'stock',
					movements
						.map((m) => `<tr><td>${esc(m.product_title)}</td><td>${esc(m.sku)}</td><td class="${m.delta < 0 ? 'danger' : 'warn'}">${m.delta > 0 ? '+' : ''}${m.delta}</td><td>${esc(moveReason(m.reason))}</td><td>${esc(dt(m.created_at))}</td></tr>`)
						.join('') || none(5)
				);
			},
			orders: async () => {
				const form = $('#orderSearch');
				const params = new URLSearchParams({ q: form.q.value.trim(), status: form.status.value, limit: '30' });
				const data = await api.get(`/api/admin/orders?${params}`);
				rows(
					'order',
					data.items
						.map(
							(o) => `<tr>
							<td><a href="/order/${esc(o.order_no)}">${esc(o.order_no)}</a></td>
							<td>${esc(o.status_text)}</td>
							<td>${money(o.total_cents)}</td>
							<td>${o.items.reduce((n, i) => n + i.qty, 0)} 件</td>
							<td>${esc(o.tracking_no || '—')}</td>
							<td class="row-act">
								${o.status === 'paid' ? act('发货', 'ship', o.order_no) : ''}
								${o.status === 'shipped' ? act('标记送达', 'deliver', o.order_no) : ''}
								${o.status === 'delivered' ? act('交易完成', 'complete', o.order_no) : ''}
							</td>
						</tr>`
						)
						.join('') || none(6)
				);
			},
			refunds: async () => {
				const form = $('#refundSearch');
				const params = new URLSearchParams({ q: form.q.value.trim(), status: form.status.value, limit: '30' });
				const data = await api.get(`/api/admin/refunds?${params}`);
				rows(
					'refund',
					data.items
						.map(
							(r) => `<tr>
							<td>${esc(r.id)}<span class="muted"> · ${esc(dt(r.created_at))}</span></td>
							<td><a href="/order/${esc(r.order_no)}">${esc(r.order_no)}</a><span class="muted"> · ${esc(r.email || '')}</span></td>
							<td>${esc(r.kind_text)} · ${money(r.amount_cents)}<span class="muted"> / 可退 ${money(r.refundable_cents)}</span></td>
							<td>${esc(r.reason)}</td>
							<td class="${r.status === 'pending' ? 'warn' : ''}">${esc(r.status_text || r.status)}${r.admin_note ? `<span class="muted"> · ${esc(r.admin_note)}</span>` : ''}</td>
							<td class="row-act">${r.status === 'pending' ? `${act('同意退款', 'refund-approve', r.id)} ${act('驳回', 'refund-reject', r.id)}` : '—'}</td>
						</tr>`
						)
						.join('') || none(6)
				);
			},
			members: async () => {
				const form = $('#memberSearch');
				const params = new URLSearchParams({ q: form.q.value.trim(), limit: '40' });
				const data = await api.get(`/api/admin/members?${params}`);
				rows(
					'member',
					data.items
						.map(
							(m) => `<tr>
							<td>${esc(m.name)}<span class="muted"> · ${esc(m.role)}</span></td>
							<td>${esc(m.email)}<span class="muted"> ${esc(m.phone || '')}</span></td>
							<td>${m.orders}</td>
							<td>${money(m.paid_cents)}</td>
							<td>${money(m.balance_cents)}</td>
							<td class="row-act">${act('调整余额', 'balance', m.id)}</td>
						</tr>`
						)
						.join('') || none(6)
				);
			},
			faqs: async () => {
				const items = await api.get('/api/admin/faqs');
				rows(
					'faq',
					items
						.map(
							(f) => `<tr>
							<td>${esc(f.question)}</td>
							<td>${esc(f.kind)}</td>
							<td>${f.hits}</td>
							<td>${f.active ? '启用' : '停用'}</td>
							<td class="row-act">${act('编辑', 'faq-edit', f.id)} ${act('删除', 'faq-del', f.id)}</td>
						</tr>`
						)
						.join('') || none(5)
				);
			},
			agent: async () => {
				const form = $('#threadSearch');
				const params = new URLSearchParams({ q: form.q.value.trim(), status: form.status.value, limit: '30' });
				const data = await api.get(`/api/admin/agent/threads?${params}`);
				$('#threadList').innerHTML = data.items
					.map(
						(t) => `<li data-id="${esc(t.id)}"><b>${esc(t.last_question || t.intent_text || '新会话')}</b><span>${esc(t.name || t.email || '游客')} · ${esc(t.intent_text || '会话')} · ${t.user_turns || 0} 轮 · ${esc(dt(t.updated_at))}</span></li>`
					)
					.join('');
				$$('#threadList li').forEach((li) => {
					li.addEventListener('click', async () => {
						$$('#threadList li').forEach((other) => other.classList.toggle('on', other === li));
						const detail = await api.get(`/api/admin/agent/threads/${li.dataset.id}`);
						const images = (detail.images || []).length
							? `<p class="admin-images">上传的图 ${detail.images.length} 张：` +
								detail.images
									.map((im) => `<a href="${esc(im.url)}" target="_blank" rel="noopener"><img src="${esc(im.url)}" alt="${esc(im.filename || '上传图')}"><em>${esc(im.filename || '')} ${im.width}×${im.height}</em></a>`)
									.join('') +
								'</p>'
							: '';
						$('#threadDetail').innerHTML =
							detail.messages.map((m) => bubbleRow(m.role === 'user' ? 'user' : 'agent', esc(m.content)).outerHTML).join('') +
							images +
							`<p class="filter-hint">工具调用 ${detail.tool_calls.length} 次：${detail.tool_calls.map((c) => `${esc(c.label || c.tool)}(${c.result_count} ${TOOL_UNITS[c.tool] || '条'} / ${c.duration_ms}ms)`).join('、') || '无'}</p>`;
					});
				});
			},
		};

		const load = (pane) => {
			if (!panes[pane]) return;
			panes[pane]().catch((err) => alert(err.message));
		};

		$$('.admin-tab').forEach((tab) => {
			tab.addEventListener('click', () => {
				$$('.admin-tab').forEach((other) => other.classList.toggle('on', other === tab));
				$$('.admin-pane').forEach((pane) => pane.classList.toggle('on', pane.dataset.pane === tab.dataset.pane));
				const pane = tab.dataset.pane;
				if (!loaded[pane]) {
					loaded[pane] = true;
					load(pane);
				}
			});
		});
		loaded.overview = true;

		$$('.admin-bar').forEach((form) => {
			form.addEventListener('submit', (event) => {
				event.preventDefault();
				const pane = form.closest('.admin-pane');
				load(pane ? pane.dataset.pane : 'faqs');
			});
		});

		const faqForm = $('#faqForm');
		if (faqForm) {
			faqForm.addEventListener('submit', async (event) => {
				event.preventDefault();
				try {
					await api.post('/api/admin/faqs', {
						id: $('#faqId').value ? Number($('#faqId').value) : null,
						question: $('#faqQ').value.trim(),
						keywords: $('#faqK').value.trim(),
						answer: $('#faqA').value.trim(),
						kind: $('#faqKind').value,
						sort: Number($('#faqSort').value || 100),
					});
					faqForm.reset();
					$('#faqId').value = '';
					load('faqs');
				} catch (err) {
					alert(err.message);
				}
			});
		}

		document.addEventListener('click', async (event) => {
			const button = event.target.closest('.mini');
			if (!button) return;
			const kind = button.dataset.kind;
			const id = button.dataset.id;
			const tr = button.closest('tr');
			try {
				if (kind === 'product') {
					await api.put(`/api/admin/products/${id}`, {
						status: $('select[data-field="status"]', tr).value,
						featured: $('input[data-field="featured"]', tr).checked,
					});
					flash($('#productHint'), `商品 ${id} 已更新。`);
				} else if (kind === 'variant') {
					await api.put(`/api/admin/variants/${id}`, {
						price_cents: Math.round(Number($('input[data-field="price_yuan"]', tr).value) * 100),
						stock_qty: Number($('input[data-field="stock_qty"]', tr).value),
					});
					flash($('#variantMsg'), `规格 ${id} 已更新。`);
					load('inventory');
				} else if (kind === 'ship' || kind === 'deliver' || kind === 'complete') {
					const payload = { status: kind === 'ship' ? 'shipped' : kind === 'deliver' ? 'delivered' : 'completed' };
					if (kind === 'ship') {
						payload.tracking_no = prompt('运单号？') || '';
						payload.carrier = prompt('承运商（默认顺丰速运）') || '顺丰速运';
					}
					await api.post(`/api/admin/orders/${id}/fulfill`, payload);
					load('orders');
				} else if (kind === 'refund-approve' || kind === 'refund-reject') {
					const note = prompt(kind === 'refund-approve' ? '给买家的说明（可留空）' : '驳回原因（必填）') || '';
					await api.post(`/api/admin/refunds/${id}/${kind === 'refund-approve' ? 'approve' : 'reject'}`, { note });
					load('refunds');
				} else if (kind === 'balance') {
					const yuan = prompt('调整金额（元，正数加、负数减）');
					if (yuan === null) return;
					await api.post(`/api/admin/members/${id}/balance`, { amount_cents: Math.round(Number(yuan) * 100), note: '后台调整' });
					load('members');
				} else if (kind === 'faq-edit') {
					const items = await api.get('/api/admin/faqs');
					const row = items.find((f) => String(f.id) === id);
					if (!row) return;
					$('#faqId').value = String(row.id);
					$('#faqQ').value = row.question;
					$('#faqK').value = row.keywords || '';
					$('#faqA').value = row.answer;
					$('#faqKind').value = row.kind;
					$('#faqSort').value = String(row.sort);
					window.scrollTo({ top: 0, behavior: 'smooth' });
				} else if (kind === 'faq-del') {
					if (!confirm('删除这条知识库问答？小马的离线大脑会立刻少一个可答问题。')) return;
					await api.del(`/api/admin/faqs/${id}`);
					load('faqs');
				}
			} catch (err) {
				alert(err.message);
			}
		});
	}

	document.addEventListener('DOMContentLoaded', () => {
		initProductPage();
		initCartPage();
		initCheckout();
		initAccount();
		initOrderPage();
		initXiaoma();
		initSupportPage();
		initAdmin();
	});
})();
