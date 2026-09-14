const numbers = new Intl.NumberFormat('ru-RU');
const statuses = {running:'Загружается',succeeded:'Завершено',waiting:'В очереди',starting:'Запускается',blocked:'Ожидание остановлено',failed:'Ошибка',interrupted:'Остановлено',not_started:'Не начато'};
const notes = {
  running:'Данные сохраняются после каждого дня. Загрузка продолжается.',
  succeeded:'Весь выбранный период загружен.',
  waiting:'Начнётся автоматически после успешной загрузки накладных.',
  starting:'Накладные загружены. Подготавливаем загрузку списаний.',
  blocked:'Сначала необходимо продолжить загрузку накладных.',
  interrupted:'Процесс остановлен. Сохранённый прогресс позволит продолжить загрузку.',
  failed:'Загрузка остановилась на следующем дне. Уже сохранённые данные доступны.',
  not_started:'Загрузка ещё не запускалась.'
};
const cards = new Map();
const date = value => value ? value.slice(0,10).split('-').reverse().join('.') : '—';
const time = value => value ? new Date(value).toLocaleString('ru-RU') : '—';
function render(job, index) {
  let card = cards.get(job.resource);
  if (!card) {
    card = document.querySelector('#job-template').content.firstElementChild.cloneNode(true);
    cards.set(job.resource, card);
  }
  const container = document.querySelector('#jobs');
  if (container.children[index] !== card) container.insertBefore(card,container.children[index] || null);
  const text = (selector,value) => { card.querySelector(selector).textContent = value; };
  card.dataset.status = job.status;
  text('.step',`ЗАГРУЗКА ${index + 1}`); text('.badge',statuses[job.status] || job.status);
  text('h2',job.title); text('.period',`${date(job.date_from)} — ${date(job.date_to)}`);
  text('.percentage > strong',`${numbers.format(job.percent)}%`);
  text('.days',`${numbers.format(job.completed_days)} из ${numbers.format(job.total_days)} дней`);
  const bar = card.querySelector('.track');
  bar.setAttribute('aria-label',job.title); bar.setAttribute('aria-valuenow',job.percent);
  card.querySelector('.fill').style.width = `${job.percent}%`;
  text('.documents',numbers.format(job.documents)); text('.items',numbers.format(job.items));
  const events = job.mode === 'events_history';
  const shifts = job.mode === 'cash_shift_history';
  const sales = job.mode === 'sales_history';
  text('.documents-label',sales ? 'Отчётов OLAP' : shifts ? 'Кассовых смен' : events ? 'Событий' : 'Документов');
  text('.items-label',sales ? 'Строк отчётов' : shifts ? 'Сопоставлено с ресторанами' : events ? 'Сопоставленных переносов RMS' : 'Строк товаров');
  text('.through',date(job.completed_through)); text('.next',date(job.next_date));
  let note = events && job.status === 'waiting'
    ? 'В очереди. По очереди загружаем один день каждого RMS.'
    : notes[job.status] || 'Проверяем состояние загрузки.';
  if (job.partial_day) note += ` День ${date(job.partial_day)} загружен не полностью: предварительные данные на ${time(job.partial_as_of)}.`;
  if ((events || shifts || sales) && job.error_code) note += ` Код ошибки: ${job.error_code}.`;
  if (sales && job.warning_days) note += ` Дней с расхождениями между отчётами: ${numbers.format(job.warning_days)}. Исходные суммы сохранены, нужна сверка.`;
  text('.note',note);
  text('.last-update',job.updated_at ? `${events ? 'Обновлено' : 'Последнее сохранение'}: ${time(job.updated_at)}` : 'Данные ещё не загружались');
}
let busy = false;
let timer;
async function refresh() {
  if (busy) return;
  clearTimeout(timer); busy = true;
  const button = document.querySelector('#refresh'); button.disabled = true;
  const controller = new AbortController();
  const timeout = setTimeout(()=>controller.abort(),8000);
  try {
    const response = await fetch('/api/v1/sync/status',{cache:'no-store',signal:controller.signal});
    if (!response.ok) throw new Error('progress_unavailable');
    const data = await response.json();
    const priority = job => job.mode === 'sales_history' ? 3 : job.mode === 'cash_shift_history' ? 2 : job.mode === 'events_history' ? 1 : 0;
    [...data.jobs].sort((a,b)=>priority(b)-priority(a)).forEach(render);
    document.querySelector('#scope').textContent = data.jobs.length && data.jobs.every(job => job.mode === 'rolling_60_days')
      ? 'Обновление открытого периода · 60 дней'
      : data.jobs.some(job => job.mode === 'sales_history') ? 'Загрузка истории продаж OLAP и документов'
      : data.jobs.some(job => job.mode === 'events_history')
        ? 'История документов, кассовых смен и событий RMS' : 'Загрузка истории документов и кассовых смен';
    document.querySelector('#failure').hidden = true;
    document.querySelector('#connection').textContent = 'Автообновление включено · каждые 5 секунд';
    document.querySelector('#checked').textContent = `Проверено: ${time(data.checked_at)}`;
  } catch {
    const error = document.querySelector('#failure');
    error.hidden = false; error.textContent = 'Не удалось обновить прогресс. На экране последние полученные значения. Повторим автоматически.';
    document.querySelector('#connection').textContent = 'Нет свежих данных';
  } finally {
    clearTimeout(timeout); busy = false; button.disabled = false;
    timer = setTimeout(refresh,5000);
  }
}
document.querySelector('#refresh').addEventListener('click',refresh);
refresh();
