import csv
import datetime
import logging
from urllib.parse import urlparse
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import IntegrityError

# Set up logging
log = logging.getLogger(__name__)

Base = declarative_base()

class VisitedUrl(Base):
    __tablename__ = 'visited_urls'
    url = Column(String, primary_key=True)
    last_hit = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

class DomainClassification(Base):
    __tablename__ = 'domain_classifications'
    domain = Column(String, primary_key=True)
    category = Column(String, nullable=True)
    state = Column(String, nullable=True)
    org_type = Column(String, nullable=True)

class Lead(Base):
    __tablename__ = 'leads'
    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False)
    source_url = Column(String)
    page_title = Column(String)
    context_snippet = Column(String)
    
    # Auto-populated metadata fields based on DomainClassification
    domain_category = Column(String, nullable=True)
    domain_state = Column(String, nullable=True)
    domain_org_type = Column(String, nullable=True)
    
    captured_at = Column(DateTime, default=datetime.datetime.utcnow)

class LocalStorage:
    """
    Manages the SQLAlchemy database connection for storing visited URLs, domain classifications, and captured leads.
    """

    def __init__(self, db_uri="sqlite:///crawler_session.db", recrawl_days=30):
        try:
            self.engine = create_engine(db_uri, echo=False)
            Base.metadata.create_all(self.engine)
            Session = sessionmaker(bind=self.engine)
            self.session = Session()
            self.recrawl_days = recrawl_days
            log.info(f"Successfully connected to database: {db_uri}")
        except Exception as e:
            log.error(f"Database connection failed: {e}")
            raise

    def is_visited(self, url: str) -> bool:
        """
        Checks if a URL has already been visited and whether it was hit recently.
        Returns True if it should be skipped (visited within recrawl_days threshold).
        """
        try:
            record = self.session.query(VisitedUrl).filter_by(url=url).first()
            if record and record.last_hit:
                time_since_hit = datetime.datetime.utcnow() - record.last_hit
                if time_since_hit.days < self.recrawl_days:
                    return True # Skip it
            return False # Not visited, or visited a long time ago
        except Exception as e:
            log.error(f"Error checking is_visited for {url}: {e}")
            return False

    def get_recently_visited_urls(self) -> set[str]:
        """Returns a set of URLs that were visited within the recrawl_days threshold."""
        try:
            threshold_date = datetime.datetime.utcnow() - datetime.timedelta(days=self.recrawl_days)
            records = self.session.query(VisitedUrl.url).filter(VisitedUrl.last_hit >= threshold_date).all()
            return {r[0] for r in records}
        except Exception as e:
            log.error(f"Error fetching recently visited URLs: {e}")
            return set()

    def mark_visited(self, url: str):
        """Marks a URL as visited in the database (updates last_hit if exists)."""
        try:
            record = self.session.query(VisitedUrl).filter_by(url=url).first()
            if record:
                record.last_hit = datetime.datetime.utcnow()
            else:
                new_record = VisitedUrl(url=url, last_hit=datetime.datetime.utcnow())
                self.session.add(new_record)
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
        except Exception as e:
            self.session.rollback()
            log.warning(f"Failed to mark URL as visited: {url} - {e}")

    def save_domain_classifications(self, classifications: dict[str, dict[str, str]]):
        """
        Bulk upserts domain classifications into the database.
        """
        log.info(f"Saving {len(classifications)} domain classifications to DB...")
        try:
            for domain, metadata in classifications.items():
                record = self.session.query(DomainClassification).filter_by(domain=domain).first()
                if record:
                    record.category = metadata.get("category")
                    record.state = metadata.get("state")
                    record.org_type = metadata.get("org_type")
                else:
                    new_record = DomainClassification(
                        domain=domain,
                        category=metadata.get("category"),
                        state=metadata.get("state"),
                        org_type=metadata.get("org_type")
                    )
                    self.session.add(new_record)
            self.session.commit()
            log.info("Domain classifications saved successfully.")
        except Exception as e:
            self.session.rollback()
            log.error(f"Failed to save domain classifications: {e}")

    def save_lead(self, email: str, source_url: str, page_title: str, context_snippet: str) -> bool:
        """Saves a new lead to the database. Returns True if a new lead was inserted.
        Automatically maps the domain to its classification."""
        
        # Extract root domain from source_url
        parsed = urlparse(source_url)
        root_domain = f"{parsed.scheme}://{parsed.netloc}"
        
        # Look up classification
        domain_cat = None
        domain_state = None
        domain_org = None
        
        try:
            classification = self.session.query(DomainClassification).filter_by(domain=root_domain).first()
            if classification:
                domain_cat = classification.category
                domain_state = classification.state
                domain_org = classification.org_type
        except Exception as e:
            log.warning(f"Failed to lookup classification for {root_domain}: {e}")

        try:
            new_lead = Lead(
                email=email,
                source_url=source_url,
                page_title=page_title,
                context_snippet=context_snippet,
                domain_category=domain_cat,
                domain_state=domain_state,
                domain_org_type=domain_org,
                captured_at=datetime.datetime.utcnow()
            )
            self.session.add(new_lead)
            self.session.commit()
            return True
        except IntegrityError:
            # Email is already in the database (UNIQUE constraint)
            self.session.rollback()
            return False
        except Exception as e:
            self.session.rollback()
            log.warning(f"Failed to save lead: {email} - {e}")
            return False

    def get_lead_count(self) -> int:
        """Returns the total number of leads in the database."""
        try:
            return self.session.query(Lead).count()
        except Exception as e:
            log.error(f"Failed to get lead count: {e}")
            return 0

    def export_to_csv(self, filename="leads.csv") -> int:
        """Exports all leads to a CSV file and returns the number of exported leads."""
        try:
            leads = self.session.query(Lead).all()
            with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
                csv_writer = csv.writer(csvfile)
                csv_writer.writerow([
                    "Email", "Source URL", "Page Title", "Context/Surrounding Text", 
                    "Category / Ministry", "State", "Organization Type", "Scraped At"
                ])
                for lead in leads:
                    csv_writer.writerow([
                        lead.email,
                        lead.source_url,
                        lead.page_title,
                        lead.context_snippet,
                        lead.domain_category or "Unknown",
                        lead.domain_state or "Unknown",
                        lead.domain_org_type or "Unknown",
                        lead.captured_at.isoformat() if lead.captured_at else ""
                    ])
            log.info(f"Exported {len(leads)} classified leads to {filename}")
            return len(leads)
        except Exception as e:
            log.error(f"Failed to export leads to CSV: {e}")
            return 0

    def close(self):
        """Closes the database session."""
        if hasattr(self, 'session'):
            self.session.close()
            log.info("Database session closed.")
