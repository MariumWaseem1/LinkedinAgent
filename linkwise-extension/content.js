(async function () {
  function t(el) {
    return el ? el.innerText.trim() : '';
  }

  function findSection(id, ariaLabel) {
    return (
      document.getElementById(id) ||
      [...document.querySelectorAll('section')].find(
        s => s.getAttribute('aria-label') && s.getAttribute('aria-label').toLowerCase().includes(ariaLabel.toLowerCase())
      )
    );
  }

  // Scroll to bottom then back to trigger lazy-loaded sections
  window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
  await new Promise(r => setTimeout(r, 900));
  window.scrollTo({ top: 0, behavior: 'smooth' });
  await new Promise(r => setTimeout(r, 600));

  // Name
  const nameEl =
    document.querySelector('h1.text-heading-xlarge') ||
    document.querySelector('h1');
  const name = t(nameEl) || 'Unknown';

  // Headline
  const headlineEl =
    document.querySelector('.text-body-medium.break-words') ||
    document.querySelector('[data-generated-suggestion-target] .text-body-medium');
  let headline = t(headlineEl);
  if (!headline) {
    const meta = document.querySelector('meta[name="description"]');
    headline = meta ? meta.getAttribute('content') || '' : '';
  }

  // About
  const aboutSection = findSection('about', 'about');
  let about = '';
  if (aboutSection) {
    const aboutText = aboutSection.querySelector('.display-flex.ph5.pv3 span[aria-hidden="true"]') ||
                      aboutSection.querySelector('span[aria-hidden="true"]');
    about = aboutText ? t(aboutText) : t(aboutSection).replace(/^About\s*/i, '').split('\n')[0];
  }

  // Experience
  const expSection = findSection('experience', 'experience');
  const experiences = [];
  if (expSection) {
    const items = expSection.querySelectorAll('li.artdeco-list__item');
    items.forEach(item => {
      const titleEl = item.querySelector('.mr1.t-bold span[aria-hidden="true"]') ||
                      item.querySelector('[class*="t-bold"] span[aria-hidden="true"]');
      const companyEl = item.querySelector('.t-14.t-normal span[aria-hidden="true"]') ||
                        item.querySelector('.t-normal span[aria-hidden="true"]');
      const datesEl = item.querySelectorAll('.t-14.t-normal.t-black--light span[aria-hidden="true"]');
      const descEl = item.querySelector('.pvs-list__item--with-top-padding span[aria-hidden="true"]');
      const title = t(titleEl);
      const company = t(companyEl);
      const dates = datesEl.length > 0 ? t(datesEl[0]) : '';
      const desc = t(descEl);
      if (title || company) {
        experiences.push({ title, company, dates, desc });
      }
    });
  }

  // Education
  const eduSection = findSection('education', 'education');
  const educations = [];
  if (eduSection) {
    const items = eduSection.querySelectorAll('li.artdeco-list__item');
    items.forEach(item => {
      const instEl = item.querySelector('.mr1.t-bold span[aria-hidden="true"]') ||
                     item.querySelector('[class*="t-bold"] span[aria-hidden="true"]');
      const degreeEl = item.querySelector('.t-14.t-normal span[aria-hidden="true"]');
      const datesEl = item.querySelector('.t-14.t-normal.t-black--light span[aria-hidden="true"]');
      const institution = t(instEl);
      const degree = t(degreeEl);
      const dates = t(datesEl);
      if (institution || degree) {
        educations.push({ institution, degree, dates });
      }
    });
  }

  // Skills
  const skillsSection = findSection('skills', 'skills');
  const skills = [];
  if (skillsSection) {
    const skillEls = skillsSection.querySelectorAll('.mr1.t-bold span[aria-hidden="true"], [class*="t-bold"] span[aria-hidden="true"]');
    skillEls.forEach(el => {
      const skill = t(el);
      if (skill && !skills.includes(skill)) skills.push(skill);
    });
  }

  // Certifications
  const certSection = findSection('certifications', 'certifications') ||
    [...document.querySelectorAll('section')].find(s =>
      s.getAttribute('aria-label') && s.getAttribute('aria-label').toLowerCase().includes('license')
    );
  const certs = [];
  if (certSection) {
    const items = certSection.querySelectorAll('li.artdeco-list__item');
    items.forEach(item => {
      const nameEl2 = item.querySelector('.mr1.t-bold span[aria-hidden="true"]');
      const issuerEl = item.querySelector('.t-14.t-normal span[aria-hidden="true"]');
      const certName = t(nameEl2);
      const issuer = t(issuerEl);
      if (certName) certs.push({ certName, issuer });
    });
  }

  // Followers and connections
  const bodyText = document.body.innerText;
  const followersMatch = bodyText.match(/([\d,]+)\s+followers/i);
  const connectionsMatch = bodyText.match(/([\d,]+)\s+connections/i);
  const followers = followersMatch ? followersMatch[1] : '';
  const connections = connectionsMatch ? connectionsMatch[1] : '';

  // Build formatted text
  const lines = [];
  lines.push(`Name: ${name}`);
  if (headline) lines.push(`Headline: ${headline}`);
  if (about) lines.push(`About: ${about}`);

  if (experiences.length > 0) {
    lines.push('Experience:');
    experiences.forEach(e => {
      const parts = [e.title, e.company ? `at ${e.company}` : '', e.dates ? `(${e.dates})` : ''].filter(Boolean).join(' ');
      const desc = e.desc ? `: ${e.desc.substring(0, 200)}` : '';
      lines.push(`- ${parts}${desc}`);
    });
  }

  if (educations.length > 0) {
    lines.push('Education:');
    educations.forEach(e => {
      const parts = [e.degree, e.institution ? `at ${e.institution}` : '', e.dates ? `(${e.dates})` : ''].filter(Boolean).join(' ');
      lines.push(`- ${parts}`);
    });
  }

  if (skills.length > 0) {
    lines.push(`Skills: ${skills.slice(0, 20).join(', ')}`);
  }

  if (certs.length > 0) {
    lines.push('Certifications:');
    certs.forEach(c => {
      lines.push(`- ${c.certName}${c.issuer ? ` by ${c.issuer}` : ''}`);
    });
  }

  if (followers) lines.push(`Followers: ${followers}`);
  if (connections) lines.push(`Connections: ${connections}`);

  const profileText = lines.join('\n');

  chrome.runtime.sendMessage({ type: 'PROFILE_EXTRACTED', profileText });
})();
